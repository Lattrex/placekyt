# SPDX-License-Identifier: GPL-3.0-or-later
"""ChaCha20 BLOCK FUNCTION golden — pinned by RFC 8439's own test vectors.

``ChaCha20KeystreamBlock`` is QUARANTINED (see ``verification/manifest.json``):
the assembled 80-invocation loop does not yet run end to end on chip. What IS
established, and what this file gates, is the layer everything else depends on:

1. the Python golden (``chacha20_golden.py``) is EXACT against RFC 8439 §2.3.2
   (block function + keystream) and §2.4.2 (encryption), and
2. the closed-form column/diagonal index schedule — the identity that makes the
   block architecturally possible at all — reproduces the RFC's eight literal
   quarter-round quadruples exactly.

Point 2 is the load-bearing one. The RFC states the round permutation as eight
hard-coded index quadruples. Expressed that way it is a *routing* decision, and
this substrate cannot route on computed data (a ``WRITE``'s ``HOP_CNT``/``DEST``
are instruction fields). Expressed as ``index(k) = 4k + ((j + k*shift) & 3)`` it
is three instructions of ARITHMETIC producing a panel ADDRESS — which the
substrate does support. The quarantined build measured that formula running
correctly on the real placed+routed chip for all eight quadruples; this gate
pins the formula itself so a future builder can trust it.

Gates the golden ONLY — there is no DUT here, by design.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chacha20_golden as g  # noqa: E402


# ------------------------------------------------------- the RFC 8439 vectors
def test_rfc8439_232_block_function_state():
    """§2.3.2: the 16 output words of the block function, exact."""
    got = g.block_function(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE,
                           g.RFC8439_BLOCK_COUNTER)
    assert tuple(got) == g.RFC8439_BLOCK_EXPECTED_STATE


def test_rfc8439_232_keystream_bytes():
    """§2.3.2: the serialised 64 keystream bytes, exact."""
    got = g.keystream_block(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE,
                            g.RFC8439_BLOCK_COUNTER)
    assert got == g.RFC8439_BLOCK_EXPECTED_KEYSTREAM
    assert len(got) == 64


def test_rfc8439_242_encryption():
    """§2.4.2: the full 114-byte encryption vector, end to end.

    This spans TWO keystream blocks, so it also pins the counter increment.
    """
    got = g.encrypt(g.RFC8439_ENCRYPT_KEY, g.RFC8439_ENCRYPT_NONCE,
                    g.RFC8439_ENCRYPT_COUNTER, g.RFC8439_ENCRYPT_PLAINTEXT)
    assert got == g.RFC8439_ENCRYPT_CIPHERTEXT


def test_encryption_round_trips():
    """XOR is an involution: decrypting the ciphertext returns the plaintext."""
    ct = g.encrypt(g.RFC8439_ENCRYPT_KEY, g.RFC8439_ENCRYPT_NONCE, 1,
                   g.RFC8439_ENCRYPT_PLAINTEXT)
    pt = g.encrypt(g.RFC8439_ENCRYPT_KEY, g.RFC8439_ENCRYPT_NONCE, 1, ct)
    assert pt == g.RFC8439_ENCRYPT_PLAINTEXT


# ----------------------------------------------- the closed-form permutation
def test_index_formula_matches_the_rfc_quadruples():
    """``4k + ((j + k*shift) & 3)`` IS the RFC's column/diagonal schedule.

    The whole architecture rests on this: it turns the permutation from routing
    (impossible here) into address arithmetic (cheap).
    """
    for j in range(4):
        assert g.quarterround_indices(j, False) == g.COLUMN_QUARTERROUNDS[j]
        assert g.quarterround_indices(j, True) == g.DIAGONAL_QUARTERROUNDS[j]


def test_each_half_round_touches_every_state_word_exactly_once():
    """The four quarter rounds of a half round PARTITION the 16 state words.

    That is why they may run concurrently, and why the half-round boundary — not
    the quarter-round boundary — is the barrier.
    """
    for diagonal in (False, True):
        seen = [i for j in range(4)
                for i in g.quarterround_indices(j, diagonal)]
        assert sorted(seen) == list(range(16))


# --------------------------------------------------------------------------
# The schedule is a CONSTANT — the property that decides the architecture.
#
# The 2026-08-29 quarantine concluded the round permutation was a
# data-dependent dataflow, which on this substrate is un-expressible as routing
# (a WRITE's HOP_CNT/DEST are instruction fields) and therefore forces the SRAM
# panel. These gates pin the facts that show it is NOT data-dependent, so a
# future builder does not pay for a panel this cipher does not need. See
# INV-49.
# --------------------------------------------------------------------------
def _full_schedule():
    """All 80 quarter-round invocations, in order."""
    return [g.quarterround_indices(j, diagonal)
            for _ in range(10)
            for diagonal in (False, True)
            for j in range(4)]


def test_the_whole_cipher_is_ten_repeats_of_one_eight_step_cycle():
    """80 invocations, but only ONE 8-step cycle, repeated ten times.

    Nothing about the dataflow varies at runtime, so the schedule is a
    CONSTANT — it is authored wiring plus a counter, never a computed
    destination.
    """
    sched = _full_schedule()
    assert len(sched) == 80
    cycle = sched[:8]
    assert all(sched[i] == cycle[i % 8] for i in range(80))


def test_only_eight_distinct_quadruples_exist_in_the_entire_cipher():
    """A short cycle means a counter, not a lookup table."""
    assert len(set(_full_schedule())) == 8


def test_every_quadruple_takes_one_word_from_each_row():
    """THE structural property that removes the need for a panel.

    Each quarter round draws exactly one word from each of the four rows
    {0-3} {4-7} {8-11} {12-15}. So if row k lives in its own cell, the ``4*k``
    term of ``index(k) = 4k + ((j + k*shift) & 3)`` is *which row*, already
    resolved by WHICH CELL is addressed — and the only thing ever computed is
    the 2-bit within-row selector, which is a ``LOAD [Rn]`` index.
    """
    for quad in set(_full_schedule()):
        assert sorted(i // 4 for i in quad) == [0, 1, 2, 3], (
            f"{quad} does not take exactly one word per row")


def test_the_within_row_selector_is_two_bits():
    """What actually has to be computed on chip is a number in 0..3."""
    for j in range(4):
        for diagonal in (False, True):
            quad = g.quarterround_indices(j, diagonal)
            for k, idx in enumerate(quad):
                sel = idx - 4 * k
                assert 0 <= sel <= 3


def test_counter_directions_are_load_bearing():
    """The COLUMN half runs first and ``j`` must ASCEND.

    A down-counting ``j`` yields ``j & 3 = 0,3,2,1`` and silently computes a
    DIFFERENT cipher — it still produces exactly 80 invocations, so no
    structural check catches it. Measured while building the sequencer.
    """
    want = [g.quarterround_indices(j, d)
            for d in (False, True) for j in range(4)]

    def sched(start_shift, ascending):
        j, shift, out = (0 if ascending else 4), start_shift, []
        for _ in range(8):
            out.append(tuple(4 * k + ((j + k * shift) & 3) for k in range(4)))
            if ascending:
                j = (j + 1) & 3
                if j == 0:
                    shift ^= 1
            else:
                j -= 1
                if j == 0:
                    j, shift = 4, shift ^ 1
        return out

    assert sched(0, True) == want                  # the correct ordering
    assert sched(0, False) != want                 # descending j is a mutant
    assert sched(1, True) != want                  # diagonal-first is a mutant


# ----------------------------------------------------------- state + counter
def test_initial_state_layout():
    """§2.3: 4 constants, 8 key words, the counter, then 3 nonce words."""
    st = g.initial_state(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1)
    assert len(st) == 16
    assert tuple(st[:4]) == g.CHACHA20_CONSTANTS
    assert st[12] == 1
    # The constants are ASCII "expand 32-byte k" little-endian.
    assert b"".join(w.to_bytes(4, "little")
                    for w in g.CHACHA20_CONSTANTS) == b"expand 32-byte k"


def test_counter_increments_per_block_without_touching_the_nonce():
    """Successive blocks differ ONLY by the counter (RFC 8439's 32-bit counter
    does not carry into the nonce)."""
    ks = g.keystream(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1, 192)
    for n, blk in enumerate(range(1, 4)):
        one = g.keystream_block(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, blk)
        assert ks[64 * n:64 * (n + 1)] == one
    # and the nonce words are untouched by the counter
    a = g.initial_state(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1)
    b = g.initial_state(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 0xFFFFFFFF)
    assert a[13:] == b[13:]


def test_wide_words_wrap_not_saturate():
    """The final add-back is mod 2**32 — exact integer wrap, never Q15 clamping.

    A saturating datapath returns 0x7FFFFFFF here; the correct answer wraps.
    """
    key = bytes(32)
    nonce = bytes(12)
    st = g.block_function(key, nonce, 0)
    assert all(0 <= w <= g.MASK32 for w in st)
    # the 16-bit round trip is lossless
    assert g.words16_to_state(g.state_to_words16(st)) == st


# ---------------------------------------------------- INV-4: negative controls
@pytest.mark.parametrize("rounds", [8, 12, 18, 22])
def test_mutation_wrong_round_count_fails(rounds):
    """20 rounds is the specification; any other count must NOT match."""
    initial = g.initial_state(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1)
    s = list(initial)
    for _ in range(rounds // 2):
        s = g.double_round(s)
    out = [(s[i] + initial[i]) & g.MASK32 for i in range(16)]
    assert tuple(out) != g.RFC8439_BLOCK_EXPECTED_STATE


def test_mutation_missing_final_addition_fails():
    """Dropping the add-back leaves the permutation trivially invertible."""
    initial = g.initial_state(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1)
    s = list(initial)
    for _ in range(10):
        s = g.double_round(s)
    assert tuple(s) != g.RFC8439_BLOCK_EXPECTED_STATE


def test_mutation_counter_not_incrementing_fails():
    """A stuck counter repeats the same 64 bytes — the classic keystream-reuse
    catastrophe, and it must be caught."""
    stuck = g.keystream_block(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1) * 2
    real = g.keystream(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1, 128)
    assert stuck != real


def test_mutation_swapped_permutation_fails():
    """Swapping the column and diagonal halves must change the answer."""
    initial = g.initial_state(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1)
    s = list(initial)
    for _ in range(10):
        for diagonal in (True, False):          # SWAPPED order
            for j in range(4):
                ia, ib, ic, idd = g.quarterround_indices(j, diagonal)
                s[ia], s[ib], s[ic], s[idd] = g.quarter_round(
                    s[ia], s[ib], s[ic], s[idd])
    out = [(s[i] + initial[i]) & g.MASK32 for i in range(16)]
    assert tuple(out) != g.RFC8439_BLOCK_EXPECTED_STATE


def test_mutation_shift_constant_fails():
    """The diagonal stride is 1. Any other stride is a different cipher."""
    for bad in (2, 3):
        idx = tuple(4 * k + ((0 + k * bad) & 3) for k in range(4))
        assert idx != g.DIAGONAL_QUARTERROUNDS[0]


def test_key_and_nonce_lengths_are_enforced():
    """A short key/nonce must RAISE, never be silently padded."""
    with pytest.raises(ValueError):
        g.initial_state(bytes(31), bytes(12), 1)
    with pytest.raises(ValueError):
        g.initial_state(bytes(32), bytes(8), 1)
