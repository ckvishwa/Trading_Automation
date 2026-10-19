# ADR 0001 -- Build the engine in Python alongside the existing Node collector

* Status: Accepted
* Date: 2026-10-19
* Milestone: M0

## Context

The repository already contained a working Node.js signal collector
(`nifty-scalper-mvp`): a loopback `node:http` webhook listener, a contract
validator, a JSONL journal, a Playwright viewer, and six passing tests. The
agreed V1 stack for the behavioural engine is Python 3.12 + FastAPI +
NumPy/pandas + SQLite/Parquet + pytest.

The milestones that carry the actual strategy -- behavioural features, pattern
similarity, walk-forward evaluation, chronological replay -- are all
numeric-research work. Node has no comparable equivalent of pandas/NumPy for
this, and reimplementing that stack in Node would be a far larger undertaking
than reimplementing the ~100 lines of contract validation in Python.

## Decision

Build the engine in Python under `app/`, and **leave the Node project exactly
where it is, untouched**. Carry forward the Node validator's rules (symbol
allowlist, timeframe allowlist, delivery-age ceiling, restart-safe dedup,
session awareness) rather than its code.

The Python receiver accepts the Node collector's field names and direction
wordings as aliases, so alerts already configured on the owner's TradingView
chart keep working without being recreated.

## Alternatives considered

1. **Extend the Node project and use Python only for research.** Rejected:
   splits the signal contract across two languages, so the rules that decide
   whether a signal is valid would have to be kept in sync by hand in the live
   path and the replay path -- exactly where a divergence is most expensive.
2. **Port the engine to Node entirely.** Rejected: no pandas/NumPy equivalent,
   and pytest-based chronological replay is the agreed research approach.
3. **Delete the Node project.** Rejected: it works, it is the owner's, and
   deleting working code without evidence or authorization is out of scope.

## Consequences

* Two codebases coexist until the owner decides to retire the Node collector.
  They are independent: different ports (8787 vs 8788), different stores
  (`data/accepted-signals.jsonl` vs `data/engine.db`), different tokens
  (`ALERT_KEY` vs `WEBHOOK_TOKEN`).
* Running both at once would double-count the same TradingView alert in two
  separate journals. Point the TradingView alert at one of them.
* `npm test` and `pytest` are both part of the suite until the Node collector is
  retired.
