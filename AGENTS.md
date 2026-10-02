# AI and Contributor Instructions

This file is the canonical working agreement for coding agents and human contributors.

## Read this context first

1. `README.md` — what the scout does and how to run it.
2. `ARCHITECTURE.md` — data flow, module boundaries, and invariants.
3. `ROADMAP.md` — deliberate future improvements and sequencing.
4. `CODEBASE_MAP.md` — generated structural index of production Python.
5. Relevant tests under `tests/` — executable behavior and regression cases.

## Project constraints

- Python 3.11+.
- Prefer the standard library unless a dependency has a clear maintenance payoff.
- Ruff is the formatter/linter, mypy runs in strict mode, and coverage requires 100% statement and branch coverage.
- `scout_bounties.py` stays close to the upstream paid-bounty scanner so upstream merges remain low-conflict.
- Fork-owned implementation lives under `bountyscout/`; strategic policy lives under `bountyscout/strategic/`. Root `opportunity_scout.py` is only the stable executable shim.
- Never weaken paid-bounty verification while changing strategic discovery.
- Treat existing seen-state corruption conservatively: malformed, unreadable, or unsupported state must fail the run rather than silently becoming empty.
- Advance seen-state only after successful complete delivery; incomplete combined verification and failed GitHub report auto-close must leave state unchanged.
- Keep `bountyscout.state` branch-agnostic. Git/worktree/`scout-state` transport belongs to the workflow, not Python state code.
- Never equate a lifecycle check failure, malformed response, auth/rate-limit error, or GitHub 404 with a confirmed closed issue. Only confirmed `closed` lifecycle evidence may prune a GitHub seen-state entry.
- Keep seen-state maintenance bounded and deterministic. Do not add blind TTL expiry or unbounded first-run revalidation; non-GitHub entries stay seen until they have an explicit platform lifecycle policy.
- Treat `last_checked_at` as the time a maintenance attempt was made, not proof that lifecycle confirmation succeeded.

## Design rules

- Give each function one clear responsibility. Prefer functions that fit comfortably on one screen; split a function when it mixes policy, I/O, parsing, scoring, or formatting.
- Keep network and filesystem side effects at the edges. Put matching, scoring, normalization, and policy decisions in pure functions when practical.
- New domain logic belongs in the smallest cohesive module that owns that concept. Leaf/domain modules must not import root `opportunity_scout.py` or reach back into `bountyscout.app`.
- Use explicit type hints on function signatures and meaningful domain names. Prefer `Mapping` for read-only mapping inputs.
- Put broadly shared stable mapping records and literals in `bountyscout/types.py`. Keep raw external JSON dynamic until validated, then prefer canonical domain types over repeated `dict[str, Any]` / `Mapping[str, Any]` interfaces.
- Avoid new mutable global state. Static configuration constants are fine; shared runtime state needs an explicit reason and synchronization where applicable.
- Docstrings should explain intent, invariants, or surprising behavior. Do not add ceremonial `Args`/`Returns` sections that only repeat obvious type hints.
- Preserve behavior during refactors. Move code first; change behavior in a separate, test-backed step when possible.
- Add regression tests for every functional bug or scanner false positive/negative that motivates a change.
- Scanner policy modules should have direct unit regressions for their decision boundaries, plus orchestration-level coverage when composition changes.
- Unit tests must not depend on live network access.

## Change protocol

Before calling a change complete:

1. Run `make format` if Python changed.
2. Run `make quality`.
3. Run `make map` whenever production Python symbols or module boundaries change, then commit `CODEBASE_MAP.md`.
4. Update `ARCHITECTURE.md` when responsibilities, dependencies, data flow, or invariants change.
5. Update `README.md` or `CONTRIBUTING.md` when user-facing commands or contributor workflow changes.

`CODEBASE_MAP.md` is generated. Do not edit it by hand.
