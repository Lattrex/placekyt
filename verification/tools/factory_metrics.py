# SPDX-License-Identifier: GPL-3.0-or-later
"""factory_metrics — the paper-metrics record for one autonomous block build.

The block library is grown by autonomous agents (see ``AGENTS.md``). For the paper we
must measure the WORKFLOW itself, not just the DSP result: how much it costs an AI
agent to take a block from "name + GNU Radio reference" to "BER-0-verified drop-in".

This module writes one ``verification/reports/factory/<KyttarBlock>.factory.json`` per
block, capturing:

  * ``tokens``          — subagent token usage (input/output/cache), from the harness's
                          own per-run accounting (NOT self-reported by the agent, so it
                          can't be fudged),
  * ``turns``           — the builder agent's tool-call count,
  * ``walltime_sec``    — prompt-to-verified wall-clock,
  * ``human_interventions`` + ``intervention_reasons`` — how many times a human/orchestrator
                          had to nudge (0 = fully autonomous),
  * ``prompts``         — the exact prompts used (initial + any nudges), for the paper's
                          reproducibility appendix,
  * ``attempts``        — build/verify iterations,
  * ``verify_passed`` / ``quarantined`` — the outcome,
  * ``commit_sha``      — the commit that shipped the block (or None).

The ORCHESTRATOR writes this (via :func:`record`), bracketing each dispatched build —
the builder agent never touches it. The DSP-correctness record stays in
``verification/reports/<KyttarBlock>.json`` (the dashboard's source); this file is the
orthogonal *cost* record, aggregated for the paper by ``gen_paper_table.py``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

FACTORY_DIR = Path(__file__).resolve().parents[1] / "reports" / "factory"

SCHEMA_VERSION = 1


@dataclass
class Prompt:
    """One prompt sent to the builder agent. ``role``: 'initial' | 'nudge' | 'resume'."""

    role: str
    text: str


@dataclass
class Tokens:
    """Token usage for the build, from the harness's per-run accounting."""

    input: int = 0
    output: int = 0
    cache_read: int = 0

    @property
    def total(self) -> int:
        return int(self.input) + int(self.output)


@dataclass
class FactoryRecord:
    """The full paper-metrics record for one block build."""

    block: str                          # KyttarBlock name (matches manifest.kyttar_block)
    grc_block: str = ""                 # its GNU Radio counterpart (manifest.grc_block)
    tier: int = 0
    wave: int = 0                       # factory wave (1 easy / 2 medium / 3 human-in-loop)
    schema_version: int = SCHEMA_VERSION

    started_utc: str = ""               # ISO8601 (orchestrator stamps these — the sim
    ended_utc: str = ""                 # forbids Date.now(); pass real timestamps in)
    walltime_sec: float = 0.0

    tokens: Tokens = field(default_factory=Tokens)
    turns: int = 0                      # builder agent tool-call count
    attempts: int = 1                   # build/verify iterations

    human_interventions: int = 0
    intervention_reasons: list[str] = field(default_factory=list)
    prompts: list[Prompt] = field(default_factory=list)

    verify_passed: bool = False
    quarantined: bool = False
    quarantine_reason: str = ""
    commit_sha: str | None = None
    notes: str = ""

    def path(self) -> Path:
        return FACTORY_DIR / f"{self.block}.factory.json"

    def to_dict(self) -> dict:
        d = asdict(self)
        # asdict turns Tokens into a plain dict already; add the derived total.
        d["tokens"]["total"] = self.tokens.total
        return d


def record(rec: FactoryRecord) -> Path:
    """Write ``rec`` to its factory.json (creating the dir), return the path."""
    FACTORY_DIR.mkdir(parents=True, exist_ok=True)
    p = rec.path()
    p.write_text(json.dumps(rec.to_dict(), indent=2) + "\n")
    return p


def load(block: str) -> FactoryRecord | None:
    """Load an existing factory record for ``block``, or None."""
    p = FACTORY_DIR / f"{block}.factory.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    tok = d.get("tokens", {}) or {}
    return FactoryRecord(
        block=d["block"], grc_block=d.get("grc_block", ""), tier=d.get("tier", 0),
        wave=d.get("wave", 0),
        started_utc=d.get("started_utc", ""), ended_utc=d.get("ended_utc", ""),
        walltime_sec=float(d.get("walltime_sec", 0.0)),
        tokens=Tokens(input=int(tok.get("input", 0)),
                      output=int(tok.get("output", 0)),
                      cache_read=int(tok.get("cache_read", 0))),
        turns=int(d.get("turns", 0)), attempts=int(d.get("attempts", 1)),
        human_interventions=int(d.get("human_interventions", 0)),
        intervention_reasons=list(d.get("intervention_reasons", [])),
        prompts=[Prompt(**p) for p in d.get("prompts", [])],
        verify_passed=bool(d.get("verify_passed", False)),
        quarantined=bool(d.get("quarantined", False)),
        quarantine_reason=d.get("quarantine_reason", ""),
        commit_sha=d.get("commit_sha"), notes=d.get("notes", ""))
