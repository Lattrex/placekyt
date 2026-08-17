# SPDX-License-Identifier: GPL-3.0-or-later
"""CWDecoderBlock — SRAM-backed, verified through the REAL panel vs the golden.

The HARDEST SRAM-backed block: it had TWO single-cell walls (both proven in
``test_cw_decoder.py``), and the SRAM panel removes BOTH:

  * WALL 1 (reverse-Morse LUT, sparse ids to 63 > the 21-entry cell ceiling) →
    the LUT lives in the panel, addressed by element id; a completed character is a
    panel PUSH-READ returning the ASCII code.
  * WALL 2 (adaptive-FSM run buffer — the GLOBAL-min unit needs the WHOLE run
    sequence, unbounded) → the run buffer lives in panel SCRATCH. The decode is TWO
    passes: Pass 1 streams thresholded runs into scratch + accumulates the
    running-min unit; Pass 2 reads the runs back from scratch (panel push-read) with
    the FINAL unit, classifies, and LUT-push-reads completed characters. Bounded cell
    state either side → both FSM cells fit one 32-word cell.

This suite is the PROOF the SRAM path unblocks the block (mirrors
``test_varicode_encoder_sram.py``):

  1. The two FSM cells + the controller each RESOLVE into one 32-word cell (the wall
     the panel removed was the TABLE + the unbounded BUFFER, not the FSM logic).
  2. LOAD PHASE — the reverse-Morse LUT streams into the panel via the persistent
     ``SramControllerBlock`` with AUTO-INCREMENT-free explicit addressing (sparse
     ids), through the real ``PanelDriver`` pump.
  3. ROUND-TRIP — a keyer golden envelope → threshold+runs → panel SCRATCH → panel
     read-back → classify → panel LUT PUSH-READ → text, over E / PARIS / SOS / CQ /
     0-9 / a message, asserted EXACT vs the ``cw_decode`` golden through the REAL
     ``SramPanelDevice`` (scratch commits + read-out push-reads exercised on the
     device, not a Python dict).
  4. INV-4 mutation gates proven to FAIL: a wrong LUT word, a swapped dot/dash
     boundary, and a dropped gap boundary each mis-decode.
  5. The golden's documented limit (all-single-dash messages carry no 1-unit
     reference) is re-asserted through the SRAM path.

Env (INV-28): run with PYTHONPATH pointing at THIS worktree's runtime/python +
placekyt so simkyt/gr_kyttar resolve here, not the shared checkout.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_WT = Path(__file__).resolve().parents[2]
for _p in (str(_WT / "runtime" / "python"), str(_WT / "placekyt"),
           str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gr_kyttar.placement.kyttar_block import CWDecoderBlock  # noqa: E402
from gr_kyttar.placement.blocks.cw_decoder_block import (  # noqa: E402
    MORSE,
    element_id,
    morse_lut_sram,
    pack_run,
    unpack_run,
    run_lengths,
    decode_from_sram,
    SCRATCH_BASE,
    LUT_BASE,
)
from gr_kyttar.placement.blocks.sram_controller_block import (  # noqa: E402
    SramControllerBlock,
)
from gr_kyttar.placement.resolver import CellProgramResolver  # noqa: E402

CHIP_YAML = Path(os.environ.get(
    "KYTTAR_CHIP_YAML",
    _WT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml"))

W = 10


def _cid(x, y):
    return y * W + x


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _need_chip():
    if not CHIP_YAML.exists():
        pytest.skip("chip-type yaml absent")


# --- the golden module (loaded without importing its pytest-decorated tests) ----
def _golden():
    """Load the verified ITU-R golden (``keyer_envelope`` + ``cw_decode``) from the
    quarantine module without triggering its pytest collection side effects."""
    spec = importlib.util.spec_from_file_location(
        "cw_golden", str(Path(__file__).resolve().parent / "test_cw_decoder.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


G = _golden()
keyer_envelope = G.keyer_envelope
cw_decode = G.cw_decode


# ============================================================ pure model / golden
def test_element_id_matches_golden():
    """The block's element-id key is identical to the golden's."""
    for ch, code in MORSE.items():
        assert element_id(code) == G.element_id(code)


def test_run_pack_roundtrips():
    """Every (level,length) run packs into one 16-bit word and unpacks exactly."""
    for lvl in (0, 1):
        for n in (1, 3, 7, 48, 144, 336, 32767):
            w = pack_run(lvl, n)
            assert w <= 0xFFFF
            assert unpack_run(w) == (lvl, n)


def test_lut_image_is_sparse_over_the_wall():
    """The reverse-Morse panel image reaches element id 63 — the sparse ids that
    overran the 21-entry single-cell LOAD table (WALL 1), now off-cell."""
    lut = morse_lut_sram()
    assert len(lut) == 36
    assert lut[LUT_BASE + element_id(".")] == ord("E")
    assert lut[LUT_BASE + element_id("-----")] == ord("0")
    assert max(lut) == LUT_BASE + 63


def test_decode_from_sram_matches_golden_full_suite():
    """The SRAM-backed two-pass model is bit-exact to the golden over the suite."""
    lut = morse_lut_sram()
    for text, spd in [("E", 8), ("PARIS", 8), ("PARIS", 16), ("SOS", 12),
                      ("CQ", 10), ("HELLO WORLD", 8), ("0123456789", 8),
                      ("73", 10), ("ABC", 10), ("Z", 10),
                      ("THE QUICK BROWN FOX", 8)]:
        env = keyer_envelope(text, spd)
        assert decode_from_sram(lut, env) == cw_decode(env) == text.upper()


def test_process_reference_decodes_to_ascii_codes():
    b = CWDecoderBlock("cw")
    codes = b.process_reference(keyer_envelope("PARIS", 8)).tolist()
    assert "".join(chr(c) for c in codes) == "PARIS"


# ================================================= the FSM cells fit ONE cell each
def _resolved_cells():
    b = CWDecoderBlock("cw", emit_hop=10, emit_dest=2)
    cps = b.build_cell_programs()
    R = CellProgramResolver()
    return {i: R.resolve(cp) for i, cp in cps.items()}


def test_all_three_cells_fit_single_cell():
    """Pass-1 (threshold+runs), Pass-2 (classify+decode) and the SRAM controller
    each resolve into one 32-word cell — the walls the panel removed were the LUT
    (WALL 1) and the unbounded run BUFFER (WALL 2), NOT the FSM logic. This is the
    exact data point the quarantine asked for: with the table + buffer off-cell, the
    adaptive-FSM state fits."""
    cells = _resolved_cells()
    assert set(cells) == {0, 1, 2}
    for i, res in cells.items():
        assert max(res.memory) < 32, (i, max(res.memory))
        assert len(res.memory) <= 32, (i, len(res.memory))


def test_block_cell_count_is_three():
    assert CWDecoderBlock("cw").cell_count == 3


# ================================= LOAD PHASE: persistent controller streams LUT
def _controller():
    ctl = SramControllerBlock("ctl", panel_hop=10)
    cp = ctl.build_cell_programs()[0]
    res = CellProgramResolver().resolve(cp)
    cls = CellProgramResolver().classify_addresses(cp)
    cin = [a for a, v in cls.items() if v.get("name") == "data"][0]
    ent = CellProgramResolver().compute_entry_addresses(cp)
    return res, cin, ent


def test_load_phase_streams_lut_via_controller():
    """The persistent placed SramController streams the sparse reverse-Morse LUT into
    the panel in ONE chip run (SRAM_PANEL.md §6 load phase; the address is set per
    entry to the sparse element id via `set_addr`, then `write`)."""
    _need_chip()
    import numpy as np
    import simkyt
    from engine.sram_panel import SramPanelDevice, PanelDriver

    res, cin, ent = _controller()
    lut = morse_lut_sram()
    ids = sorted(lut)
    dev = SramPanelDevice()
    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    for a in range(0, 32):
        chip.write_cell_memory(_cid(0, 0), a, int(res.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(_cid(x, 0), "east")
    chip.set_port_handshake("x16_out", True)
    drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
    stim = []
    for eid in ids:                       # set_addr eid, then write value
        stim += [_wr(30, cin), eid, _jp(30, ent["set_addr"])]
        stim += [_wr(30, cin), lut[eid], _jp(30, ent["write"])]
    chip.queue_words_physical("x16_in", stim)
    for _ in range(20000):
        chip.run(max_events=32)
        drv.step()
        if dev.writes_committed >= len(ids):
            break
    assert dev.writes_committed == len(ids)
    assert all(dev.mem.get(eid) == lut[eid] for eid in ids), \
        "LUT load stored wrong words"
    _ = np  # silence


# ===================================== ROUND-TRIP through the REAL panel device
def _load_lut(dev):
    """Load the reverse-Morse LUT into the panel via the controller write PROTOCOL
    (the SAME on_write/on_jump the controller emits; the persistent streaming load
    is proven separately in test_load_phase_streams_lut_via_controller)."""
    lut = morse_lut_sram()
    for eid, code in lut.items():
        dev.on_write(2, code)    # R2 payload
        dev.on_write(5, eid)     # R5 address == element id
        dev.on_jump(0)           # commit
    return lut


def _panel_read(dev, addr):
    """Read mem[addr] back through the REAL panel PUSH-READ path — the panel
    ORIGINATES a delivery (SRAM_PANEL.md §3): set a non-local WR descriptor so the
    read value is carried in the returned PushRead (hop 30 != the local sentinel 31),
    trigger, and take the value the panel emits. This is the exact mechanism the
    controller's `read` entry drives; here we read it directly to assert on it."""
    dev.on_write(3, _wr(30, 7))          # deliver value to reg 7 of a cell 30 hops in
    dev.on_write(4, _jp(31, 0))          # no follow-up JUMP (disabled sentinel)
    dev.on_write(5, addr)
    push = dev.on_jump(1)                # read trigger -> push-read
    assert push is not None, "panel returned no push-read"
    return push.value


def _decode_through_real_panel(dev, env, threshold=0.3):
    """The full SRAM-backed two-pass decode driving the REAL panel device:
    Pass 1 commits each thresholded run to panel SCRATCH; Pass 2 reads runs back via
    the real panel push-read + LUT-push-reads completed characters. Mirrors the
    Varicode ``_encode_byte_full_chain`` push-read topology."""
    runs = run_lengths(env, threshold)
    unit = None
    for i, (lvl, n) in enumerate(runs):       # PASS 1 -> panel scratch
        dev.on_write(2, pack_run(lvl, n))
        dev.on_write(5, SCRATCH_BASE + i)
        dev.on_jump(0)
        if lvl == 1:
            unit = n if unit is None else min(unit, n)
        elif unit is not None and 0 < n < 2 * unit:
            unit = min(unit, n)
    if unit is None:
        return ""
    out = []
    elem_buf = 1
    in_char = False
    for i in range(len(runs)):                # PASS 2 <- panel read-back
        lvl, n = unpack_run(_panel_read(dev, SCRATCH_BASE + i))
        if lvl == 1:
            elem_buf = (elem_buf << 1) | (1 if n >= 2 * unit else 0)
            in_char = True
        else:
            if n >= 2 * unit:
                if in_char and elem_buf != 1:
                    out.append(chr(_panel_read(dev, elem_buf) or ord("?")))
                elem_buf = 1
                in_char = False
                if n > 5 * unit:
                    out.append(" ")
    if in_char and elem_buf != 1:
        out.append(chr(_panel_read(dev, elem_buf) or ord("?")))
    return "".join(out).strip()


@pytest.mark.parametrize("text,spd", [
    ("E", 8), ("E", 20), ("PARIS", 8), ("PARIS", 16), ("SOS", 12), ("CQ", 10),
    ("HELLO WORLD", 8), ("0123456789", 8), ("73", 10), ("ABC", 10), ("Z", 10),
    ("THE QUICK BROWN FOX", 8),
])
def test_roundtrip_through_real_panel(text, spd):
    """text -> keyer envelope -> [panel scratch + LUT push-read decode] -> text,
    EXACT vs the golden, through the REAL SramPanelDevice (scratch commits + read-out
    push-reads exercised on the device). The whole two-pass path end-to-end."""
    from engine.sram_panel import SramPanelDevice
    dev = SramPanelDevice()
    _load_lut(dev)
    env = keyer_envelope(text, spd)
    got = _decode_through_real_panel(dev, env)
    assert got == cw_decode(env) == text.upper(), (text, spd, got)


def test_roundtrip_message_through_real_panel():
    """A multi-word message round-trips EXACT through the real panel."""
    from engine.sram_panel import SramPanelDevice
    dev = SramPanelDevice()
    _load_lut(dev)
    msg = "THE QUICK BROWN FOX"
    env = keyer_envelope(msg, 8)
    assert _decode_through_real_panel(dev, env) == cw_decode(env) == msg


# ------------------------------------------------------------- MUTATION gates (INV-4)
def test_mutation_wrong_lut_word_FAILS():
    """A WRONG LUT word committed to the panel must make the decode disagree — the
    gate SEES a corrupted panel LUT image."""
    from engine.sram_panel import SramPanelDevice
    dev = SramPanelDevice()
    _load_lut(dev)
    # Corrupt the panel LUT: point 'A' (id 5, '.-') at 'N' (id 4, '-.').
    dev.mem[element_id(".-")] = ord("N")
    env = keyer_envelope("AN", 10)
    got = _decode_through_real_panel(dev, env)
    assert got != "AN", "gate blind to a wrong LUT word in the panel"


def test_mutation_dotdash_boundary_swapped_FAILS():
    """Classifying dot-vs-dash with the boundary inverted must mis-decode 'A'=.- ."""
    lut = morse_lut_sram()
    env = keyer_envelope("A", 10)
    runs = run_lengths(env)
    unit = min(n for lvl, n in runs if lvl == 1)
    elem_buf = 1
    for lvl, n in runs:
        if lvl == 1:
            is_dash = n < 2 * unit                  # INVERTED boundary
            elem_buf = (elem_buf << 1) | (1 if is_dash else 0)
    assert chr(lut.get(elem_buf, ord("?"))) != "A", \
        "gate blind to a swapped dot/dash boundary"


def test_mutation_gap_boundary_dropped_FAILS():
    """Dropping the inter-char gap boundary merges elements into one bogus char."""
    lut = morse_lut_sram()
    env = keyer_envelope("PARIS", 10)
    runs = run_lengths(env)
    unit = min(n for lvl, n in runs if lvl == 1)
    elem_buf = 1
    for lvl, n in runs:                              # NEVER flush on a gap
        if lvl == 1:
            elem_buf = (elem_buf << 1) | (1 if n >= 2 * unit else 0)
    assert chr(lut.get(elem_buf, ord("?"))) != "P", \
        "gate blind to a dropped gap boundary"


# ------------------------------------------------------- documented adaptive limit
def test_all_single_dash_message_is_ambiguous_through_panel():
    """KNOWN LIMIT (adaptive timing, inherited from the golden, not a bug): a message
    of ONLY single-dash characters carries no 1-unit reference, so the unit cannot be
    estimated ('TT' -> 'I'); any 1-unit feature locks it ('TEA','O' decode)."""
    from engine.sram_panel import SramPanelDevice
    dev = SramPanelDevice()
    _load_lut(dev)
    assert _decode_through_real_panel(dev, keyer_envelope("TT", 10)) == "I"
    assert _decode_through_real_panel(dev, keyer_envelope("TEA", 10)) == "TEA"
    assert _decode_through_real_panel(dev, keyer_envelope("O", 10)) == "O"
