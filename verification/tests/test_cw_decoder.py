# SPDX-License-Identifier: GPL-3.0-or-later
"""CWDecoderBlock — CW/Morse decoder (ITU-R M.1677) — QUARANTINE guard + golden.

Status: **RESOLVED — SRAM-backed (was needs_human)**. A faithful adaptive CW/Morse
decoder does NOT fit a SINGLE Kyttar cell — this module documents that wall with an
EXECUTABLE proof and a working Python GOLDEN (round-trips a keyed envelope → text).
Both single-cell walls below still hold and stay green; the SRAM PANEL (INV-31)
removes them by moving the reverse-Morse LUT and the unbounded run buffer off-cell,
and the DUT is now real (``test_dut_now_sram_backed_and_matches_golden`` here; the
full panel round-trip is ``verification/tests/test_cw_decoder_sram.py``).

Two independent single-cell walls, both PROVEN here (not argued):

  WALL 1 — the reverse-Morse lookup exceeds the single-cell LOAD-indirect table.
    The natural dot/dash → ASCII map uses the standard "1-prefixed" element code
    (start with 1, append 0 per dot / 1 per dash). The resulting integer IDs are
    SPARSE: index 2..63 for the 36 alphanumerics (2..29 for the 26 letters, up to
    22 for even the word "PARIS"). A direct LOAD-indirect table must be indexed by
    that ID, so it needs `max(id)+1` entries: 64 (alphanumeric) / 30 (letters) /
    23 (PARIS). Every one exceeds MapBBBlock.MAX_TABLE = 21 (the empirically-proven
    single-cell ceiling: `LOAD [Rn] = mem[mem[Rn] & 0x1F]`, 32-word cell shared
    with program + scalars). `test_reverse_morse_table_exceeds_cell_budget` proves
    this arithmetically against the live MapBBBlock ceiling.

  WALL 2 — the adaptive streaming FSM's global state + table overrun 32 words.
    Unlike a FIR (a feed-forward MAC chain that pipelines across a wavefront), a CW
    decoder's state is GLOBAL and SEQUENTIAL: the adaptive dot estimate, the
    run-length counter (0..~7·samples_per_dot, needs a full 16-bit word), and the
    element-accumulation buffer are read-modify-written on EVERY sample and must be
    ONE coherent copy — they cannot be split across cells like tap slices. The
    minimal persistent FSM state is ~10 words; with scalars (~6) and the reverse
    table (≥23), that is ≥39 words of DATA before a single program instruction, on
    a 32-word cell. `test_streaming_fsm_state_exceeds_cell_budget` proves the count.

This is exactly the "long-memory accumulated-element state lives in SRAM, not cell
registers" case (KB invariants + the CWDecoder manifest note). The fix is a real
datapath redesign backed by external SRAM (sram_controller in the stream),
not a tolerance tweak — a human must scope it.

The GOLDEN below (`cw_decode`) is a correct float decoder (adaptive-unit + reverse
Morse LUT); `test_golden_roundtrip` ROUND-TRIPS text → keyer envelope → text for
'E', 'PARIS' (two speeds), digits, and multi-word phrases against the keyer envelope
model. The mutation tests prove the golden gate DISCRIMINATES (INV-4), so when a DUT
exists it is a real gate. A lone isolated dash is an inherent adaptive-timing
ambiguity, documented as a known limit (`test_isolated_lone_dash_is_ambiguous...`).
`test_dut_quarantined_xfail` is the xfail sentinel that flips green when a buildable
CWDecoderBlock lands.
"""
import pytest


# --------------------------------------------------------------------------- #
# ITU-R M.1677 International Morse Code — transcribed exactly.                 #
# Letters + digits (the canonical 36). Single source of truth for both the    #
# golden decoder and the (would-be) on-chip reverse table.                    #
# --------------------------------------------------------------------------- #
MORSE = {
    'A': '.-',    'B': '-...',  'C': '-.-.',  'D': '-..',   'E': '.',
    'F': '..-.',  'G': '--.',   'H': '....',  'I': '..',    'J': '.---',
    'K': '-.-',   'L': '.-..',  'M': '--',    'N': '-.',    'O': '---',
    'P': '.--.',  'Q': '--.-',  'R': '.-.',   'S': '...',   'T': '-',
    'U': '..-',   'V': '...-',  'W': '.--',   'X': '-..-',  'Y': '-.--',
    'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--', '4': '....-',
    '5': '.....', '6': '-....', '7': '--...', '8': '---..', '9': '----.',
}


def element_id(code: str) -> int:
    """Standard '1-prefixed' Morse element code: seed 1, append 0 per dot, 1 per
    dash. This is the compact integer key a hardware reverse-LUT would index by."""
    v = 1
    for c in code:
        v = (v << 1) | (1 if c == '-' else 0)
    return v


# id -> char, the reverse map the on-chip LOAD-indirect table would hold.
_ID_TO_CHAR = {element_id(code): ch for ch, code in MORSE.items()}


# --------------------------------------------------------------------------- #
# Keyer envelope model (the CWKeyerBlock's job) — generate the ON/OFF envelope #
# the decoder consumes, so the round-trip is text -> envelope -> text.         #
# Timing: dot=1u, dash=3u, intra-char gap=1u, inter-char gap=3u, word gap=7u.  #
# --------------------------------------------------------------------------- #
def keyer_envelope(text: str, samples_per_dot: int, amp: float = 1.0):
    """Return an ON/OFF magnitude envelope (list of floats) for `text`."""
    u = samples_per_dot
    env = []

    def on(units):
        env.extend([amp] * (units * u))

    def off(units):
        env.extend([0.0] * (units * u))

    words = text.upper().split(' ')
    for wi, word in enumerate(words):
        if wi > 0:
            off(7)  # inter-word gap
        for ci, ch in enumerate(word):
            if ch not in MORSE:
                continue
            if ci > 0:
                off(3)  # inter-char gap
            code = MORSE[ch]
            for ei, el in enumerate(code):
                if ei > 0:
                    off(1)  # intra-char gap
                on(3 if el == '-' else 1)
    off(3)  # trailing char gap to flush the final character
    return env


# --------------------------------------------------------------------------- #
# GOLDEN decoder — threshold + adaptive run-length + reverse Morse LUT.        #
# The float reference a DUT would have to match. Written in the same shape the #
# block would take: a per-sample streaming FSM with the exact persistent-state #
# set the budget analysis counts.                                             #
# --------------------------------------------------------------------------- #
def _run_lengths(envelope, threshold):
    """Threshold the envelope and return the alternating run list
    ``[(level, length), ...]`` (level 1 = key-down / ON, 0 = key-up / OFF)."""
    runs = []
    key_prev = None
    run_len = 0
    for s in list(envelope) + [0.0] * 8:   # pad to flush the trailing run
        key = 1 if s >= threshold else 0
        if key == key_prev:
            run_len += 1
        else:
            if key_prev is not None:
                runs.append((key_prev, run_len))
            key_prev = key
            run_len = 1
    runs.append((key_prev, run_len))
    return runs


def cw_decode(envelope, threshold: float = 0.3):
    """Decode an ON/OFF magnitude envelope to text (ITU-R M.1677).

    ADAPTIVE element length (the dot unit is unknown a priori — estimated from the
    signal): the ITU dot, and the intra-character gap, are BOTH exactly one time
    unit and are the SHORTEST features present. So the unit is estimated as the
    running MINIMUM of the ON-runs and the short OFF-gaps — robust regardless of
    whether the message starts with a dot or a dash (which a first-element-is-a-dot
    seed gets wrong, e.g. 'C'=-.-. or 'T'=-). With the unit locked:

      * ON-run:  dot if < 2·unit, else dash (dash ≈ 3·unit);
      * OFF-gap: intra-char if < 2·unit, inter-char if 2·unit..5·unit, word if >5·unit.

    NOTE — this needs the whole run sequence buffered to take the global minimum;
    that unbounded run buffer is precisely WALL 2 (it does not fit cell registers).
    """
    runs = _run_lengths(envelope, threshold)
    # Adaptive unit = the shortest ON-run or short OFF-gap in the message. Every
    # real character contributes at least one 1-unit feature (a dot, or the 1-unit
    # intra-char gap between its elements), so this locks the true unit.
    ons = [n for lvl, n in runs if lvl == 1]
    if not ons:
        return ''
    unit = min(ons)
    for lvl, n in runs:                    # short OFF-gaps are also 1 unit
        if lvl == 0 and 0 < n < 2 * unit:
            unit = min(unit, n)

    out = []
    elem_buf = 1
    in_char = False

    def flush_char():
        nonlocal elem_buf, in_char
        if in_char and elem_buf != 1:
            out.append(_ID_TO_CHAR.get(elem_buf, '?'))
        elem_buf = 1
        in_char = False

    for lvl, n in runs:
        if lvl == 1:
            is_dash = n >= 2 * unit
            elem_buf = (elem_buf << 1) | (1 if is_dash else 0)
            in_char = True
        else:
            if n >= 2 * unit:              # element gap ended -> a char completed
                flush_char()
                if n > 5 * unit:
                    out.append(' ')
    flush_char()
    return ''.join(out).strip()


# =========================================================================== #
# GOLDEN correctness + ROUND-TRIP (text -> keyer envelope -> decoder -> text)  #
# =========================================================================== #
@pytest.mark.parametrize("text,spd", [
    ("E", 8), ("E", 20), ("PARIS", 8), ("PARIS", 16), ("SOS", 12), ("CQ", 10),
    ("HELLO WORLD", 8), ("0123456789", 8), ("73", 10), ("ABC", 10), ("Z", 10),
    ("THE QUICK BROWN FOX", 8),
])
def test_golden_roundtrip(text, spd):
    """text -> keyer envelope -> golden decoder recovers the text exactly.

    Covers the required edges: 'E' (single dot), 'PARIS' (the WPM word) at two
    speeds, digits (all-dash '0' in context), and multi-word text (word gaps).
    """
    env = keyer_envelope(text, samples_per_dot=spd)
    assert cw_decode(env) == text.upper()


def test_golden_single_dot_E():
    """'E' = a single dot decodes (the shortest character, cold start)."""
    assert cw_decode(keyer_envelope("E", 10)) == "E"


def test_isolated_lone_dash_is_ambiguous_known_limit():
    """KNOWN LIMIT (adaptive timing, not a bug): a SINGLE isolated dash carries
    NO timing reference — with only one element and no gaps, the shortest ON-run
    IS the element, so the adaptive unit estimate cannot tell 'T'='-' from 'E'='.'.
    Real CW messages always carry a 1-unit feature (a dot, or the 1-unit intra-char
    gap inside a multi-element char) that locks the unit, so 'T' decodes correctly
    WHENEVER any such feature is present (proven below). Only a message made ENTIRELY
    of single-dash characters (e.g. 'T', 'TT') carries no 1-unit reference at all —
    no dot, and no intra-char gap (a single-element char has none) — so it is
    inherently unresolvable ('TT' reads as 'I'). A multi-element all-dash char like
    'O'='---' or 'M'='--' DOES lock the unit via its 1-unit intra-char gaps. This is
    a fundamental property of blind element-length estimation, documented not hidden."""
    assert cw_decode(keyer_envelope("T", 10)) == "E"        # lone dash: ambiguous
    assert cw_decode(keyer_envelope("TT", 10)) == "I"       # only single-dash chars
    assert cw_decode(keyer_envelope("TEA", 10)) == "TEA"    # a dot locks the unit
    assert cw_decode(keyer_envelope("TA", 10)) == "TA"      # A's dot locks the unit
    assert cw_decode(keyer_envelope("MOM", 10)) == "MOM"    # intra-char gaps lock it
    assert cw_decode(keyer_envelope("O", 10)) == "O"        # O='---' gaps lock it


def test_golden_noise_threshold_margin():
    """A modest additive bias below threshold does not corrupt the decode."""
    import random
    rng = random.Random(3)
    env = keyer_envelope("PARIS", 12, amp=1.0)
    noisy = [max(0.0, v + rng.uniform(-0.15, 0.15)) for v in env]
    assert cw_decode(noisy, threshold=0.3) == "PARIS"


# --- MANDATORY negative tests: the golden gate must DETECT corruptions (INV-4)#
def test_mutation_wrong_table_fails():
    """A corrupted reverse table (swap two entries) must mis-decode."""
    bad = dict(_ID_TO_CHAR)
    ia, ib = element_id('.-'), element_id('-.')
    bad[ia], bad[ib] = bad[ib], bad[ia]

    def decode_bad(env):
        saved = dict(_ID_TO_CHAR)
        _ID_TO_CHAR.clear(); _ID_TO_CHAR.update(bad)
        try:
            return cw_decode(env)
        finally:
            _ID_TO_CHAR.clear(); _ID_TO_CHAR.update(saved)

    assert decode_bad(keyer_envelope("AN", 10)) != "AN"


def test_mutation_dotdash_threshold_swapped_fails():
    """Classifying dot vs dash with the boundary inverted must mis-decode."""
    def decode_swapped(env, threshold=0.3):
        key_prev = run_len = dot_est = 0
        elem_buf = 1; in_char = False; out = []
        for s in list(env) + [0.0] * 8:
            key = 1 if s >= threshold else 0
            if key == key_prev:
                run_len += 1; continue
            if key_prev == 1:
                if dot_est == 0 or run_len < dot_est:
                    dot_est = run_len
                is_dash = run_len < 2 * dot_est   # INVERTED boundary
                elem_buf = (elem_buf << 1) | (1 if is_dash else 0); in_char = True
            else:
                if dot_est > 0 and run_len >= 2 * dot_est:
                    if in_char and elem_buf != 1:
                        out.append(_ID_TO_CHAR.get(elem_buf, '?'))
                    elem_buf = 1; in_char = False
            key_prev = key; run_len = 1
        if in_char and elem_buf != 1:
            out.append(_ID_TO_CHAR.get(elem_buf, '?'))
        return ''.join(out).strip()
    assert decode_swapped(keyer_envelope("A", 10)) != "A"


def test_mutation_gap_boundary_fails():
    """Dropping the inter-char gap boundary merges characters (mis-decode)."""
    def decode_nogap(env, threshold=0.3):
        # never flush on a gap -> all elements pile into one char
        key_prev = run_len = dot_est = 0; elem_buf = 1
        for s in list(env) + [0.0] * 8:
            key = 1 if s >= threshold else 0
            if key == key_prev:
                run_len += 1; continue
            if key_prev == 1:
                if dot_est == 0 or run_len < dot_est:
                    dot_est = run_len
                is_dash = run_len >= 2 * dot_est
                elem_buf = (elem_buf << 1) | (1 if is_dash else 0)
            key_prev = key; run_len = 1
        return _ID_TO_CHAR.get(elem_buf, '?')
    assert decode_nogap(keyer_envelope("PARIS", 10)) != "PARIS"


# =========================================================================== #
# THE SUBSTRATE WALL — executable proofs the block cannot fit a cell.          #
# =========================================================================== #
def test_reverse_morse_table_exceeds_cell_budget():
    """WALL 1: the reverse dot/dash → ASCII LUT overruns the single-cell table.

    A LOAD-indirect table is indexed by the 1-prefixed element ID, so it needs
    `max(id)+1` entries. Every realistic subset exceeds the proven single-cell
    ceiling MapBBBlock.MAX_TABLE = 21.
    """
    from gr_kyttar.placement.blocks.map_bb_block import MapBBBlock
    ceil = MapBBBlock.MAX_TABLE
    ids = [element_id(c) for c in MORSE.values()]
    letters = [element_id(MORSE[ch]) for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    paris = [element_id(MORSE[ch]) for ch in "PARIS"]
    assert max(ids) + 1 == 64                # full alphanumeric
    assert max(letters) + 1 == 30            # 26 letters
    assert max(paris) + 1 == 23              # even "PARIS"
    # The wall: none fit.
    assert max(paris) + 1 > ceil, "PARIS reverse table unexpectedly fit — re-open"
    assert max(letters) + 1 > ceil
    assert max(ids) + 1 > ceil


def test_streaming_fsm_state_exceeds_cell_budget():
    """WALL 2: adaptive FSM global state + reverse table overrun the 32-word cell.

    The persistent per-sample state is global/sequential (not wavefront-splittable):
    it must be one coherent copy in one cell. Even the MINIMAL single-pass EMA
    variant (no run buffer) blows the budget; the robust golden here needs the WHOLE
    run buffer (unbounded) to take the global-minimum unit estimate — strictly worse.
    Count the minimal variant against the 32-word budget.
    """
    from gr_kyttar.placement.blocks.map_bb_block import MapBBBlock
    CELL_WORDS = 32
    fsm_state_words = 10   # key_prev, run_len, dot_est, dot_acc, dot_cnt,
    #                        elem_buf, in_char, phase, out_byte, emit_flag
    scalar_words = 6       # threshold + one + zero + dot/char/word gap thresholds
    reverse_table = 23     # the MINIMUM (PARIS-only); letters=30, alnum=64
    data_words = fsm_state_words + scalar_words + reverse_table
    assert reverse_table > MapBBBlock.MAX_TABLE           # table alone over ceiling
    assert data_words > CELL_WORDS, (
        f"data words {data_words} unexpectedly fit {CELL_WORDS} — re-open the design")

    # The robust adaptive estimator this golden uses buffers EVERY run to take the
    # global minimum unit — an unbounded buffer that provably cannot live in the
    # 32-word cell (a 20-char message at samples_per_dot=48 is O(hundreds) of runs).
    runs = _run_lengths(keyer_envelope("THE QUICK BROWN FOX", 48), 0.3)
    assert len(runs) > CELL_WORDS, "run buffer unexpectedly small — re-open"
    # and this is BEFORE a single program instruction — the program is ~20-30 more.


def test_dut_now_sram_backed_and_matches_golden():
    """RESOLVED (was the xfail quarantine sentinel): a buildable, SRAM-BACKED
    CWDecoderBlock now exists and matches this golden.

    Both single-cell walls above still hold (they gate the SINGLE-CELL design and
    stay green); the SRAM PANEL removes them by moving the reverse-Morse LUT (WALL 1)
    and the unbounded run buffer (WALL 2) off-cell into the panel, with a two-pass
    scratch-buffered decode. The FULL panel round-trip proof (real SramPanelDevice /
    PanelDriver, scratch commits + LUT push-reads, bit-exact vs this ``cw_decode``)
    lives in ``verification/tests/test_cw_decoder_sram.py``. Here we assert the
    block's own ``process_reference`` matches this golden — the block is real."""
    from gr_kyttar.placement.kyttar_block import CWDecoderBlock
    b = CWDecoderBlock("cw")
    for text, spd in [("E", 8), ("PARIS", 8), ("SOS", 12), ("CQ", 10),
                      ("0123456789", 8), ("THE QUICK BROWN FOX", 8)]:
        env = keyer_envelope(text, spd)
        codes = b.process_reference(env).tolist()
        assert "".join(chr(c) for c in codes) == cw_decode(env) == text.upper()
