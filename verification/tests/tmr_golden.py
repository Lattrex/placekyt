# SPDX-License-Identifier: GPL-3.0-or-later
"""Python GOLDEN for TMRVoterBlock — triple-modular-redundancy majority vote.

There is no stock GNU Radio counterpart to compare against (``grc_block`` is
``""``): GR has no notion of redundant hardware paths, because on a host CPU
there is exactly one execution path and nothing to vote on. So this module IS
the reference, written directly from the published specification, and the
block's suite cross-checks it against the block class's own ``vote`` over the
whole interesting domain (``test_golden_matches_the_block_reference``) before
using it to judge the chip.

THE SPECIFICATION, restated here so this file stands alone:

  Three redundant chains carry what should be the SAME word. The voter compares
  all three and emits a TWO-WORD packet per sample, ``[value, status]``:

    status 0  all three agree; ``value`` is the agreed value.
    status 1  path A disagreed; ``value`` is the (correct) B/C majority.
    status 2  path B disagreed; ``value`` is the (correct) A/C majority.
    status 3  path C disagreed; ``value`` is the (correct) A/B majority.
    status 7  no two agree; ``value`` is the sentinel (default 0xFFFF).

  TMR CORRECTS a single fault: on status 1/2/3 the emitted value is still the
  majority, so a consumer that ignores the status word still gets the right
  answer. ``0xFFFF`` is outside the 0-255 byte domain, so the no-majority
  sentinel can never collide with real byte data.

Every value is a 16-bit word; comparison is on the raw word (a bit pattern),
never on a numeric interpretation — the voter is a redundancy check, so two arms
"agree" only when they are bit-identical.
"""
from __future__ import annotations

SENTINEL = 0xFFFF

STATUS_AGREE = 0
STATUS_FAULT_A = 1
STATUS_FAULT_B = 2
STATUS_FAULT_C = 3
STATUS_NO_MAJORITY = 7


def tmr_vote(a, b, c, sentinel: int = SENTINEL) -> tuple[int, int]:
    """Vote on one triple; return ``(value, status)``.

    The decision tree is written in the order the hardware evaluates it, so a
    divergence points straight at a branch:

      a == b  ->  value = a       ; status 0 if c agrees else 3 (C faulted)
      b == c  ->  value = b (= c) ; status 1 (A faulted)
      a == c  ->  value = a (= c) ; status 2 (B faulted)
      else    ->  value = sentinel; status 7 (no majority)

    Note the algebraic identity the hardware exploits: when ``a != b``, the
    majority — whenever one exists — is ALWAYS ``c``, because a majority needs
    ``c`` to equal one of them. That is why the disagree half needs no value
    selection at all.
    """
    a = int(a) & 0xFFFF
    b = int(b) & 0xFFFF
    c = int(c) & 0xFFFF
    if a == b:
        return (a, STATUS_AGREE if a == c else STATUS_FAULT_C)
    if b == c:
        return (b, STATUS_FAULT_A)
    if a == c:
        return (a, STATUS_FAULT_B)
    return (int(sentinel) & 0xFFFF, STATUS_NO_MAJORITY)


def tmr_stream(a_words, b_words, c_words,
               sentinel: int = SENTINEL) -> list[int]:
    """N complete (a, b, c) triples in; the FLAT 2-word-per-sample word stream
    ``[v0, s0, v1, s1, ...]`` out.

    A packet is emitted ONLY when ALL THREE arms have supplied their word, so an
    arm starved after ``k`` words yields exactly ``k`` packets — hence the
    truncation to the shortest arm. The ARRIVAL ORDER is deliberately absent
    from this signature: the whole point of the block's LOCK rotation is that the
    result does not depend on it, and a reference that took an order could not
    express that.
    """
    n = min(len(a_words), len(b_words), len(c_words))
    out: list[int] = []
    for i in range(n):
        v, s = tmr_vote(a_words[i], b_words[i], c_words[i], sentinel)
        out.append(v)
        out.append(s)
    return out
