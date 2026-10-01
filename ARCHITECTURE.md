# Architecture

## Goals

OSS Opportunity Scout has two lanes with different risk profiles:

- **Paid bounty lane:** conservative payment and competition verification, kept close to upstream BountyScout behavior.
- **Strategic OSS lane:** broader discovery plus project-specific readiness, competition, scoring, and ranking.

The architecture should make those lanes easy to reason about without forcing contributors or AI tools to load the full scanner into context.

## Data flow

```text
GitHub + bounty-platform sources
            |
            v
       discovery pools
            |
            v
 cheap eligibility / pre-ranking
            |
            v
 source refresh + readiness + competition verification
            |
            v
      scoring / effort model
            |
            v
 ranked queue + audit diagnostics
            |
            v
 GitHub issue / optional notifications
            |
            v
      seen_bounties.json
```

The important boundary is between **I/O** and **policy**. Network fetches gather evidence; pure functions should interpret that evidence whenever practical.

## Current modules

| Module | Responsibility | Boundary |
| --- | --- | --- |
| `scout_bounties.py` | Upstream-compatible paid discovery, payment signals, basic competition checks, notifications | Keep changes conservative to reduce upstream merge conflicts |
| `opportunity_scout.py` | Application orchestration plus strategic discovery, verification, scoring, reporting, and transitional legacy logic | Shrink over time by extracting cohesive strategic domains |
| `strategic_claims.py` | Pure first-person ownership / implementation / PR-intent language detection | No network I/O and no dependency on `opportunity_scout.py` |
| `strategic_competition.py` | Active-claim, linked/search/timeline implementation-PR detection, and competition precedence | May call upstream GitHub helpers; never imports `opportunity_scout.py` |
| `strategic_readiness.py` | Pure maintainer-readiness, triage, lifecycle, dashboard, and release-tracking policy | Interprets issue/comment evidence only; no network I/O or dependency on `opportunity_scout.py` |
| `seen_bounties.json` | Notification state | Only mark items seen after a notification path succeeds |
| `.github/workflows/bounty-scout.yml` | Scheduled scanner execution | Runtime workflow, not the quality gate |
| `.github/workflows/python-quality.yml` | Formatting, lint, map, typing, tests, coverage | Must stay fast enough for normal PR iteration |

## Dependency direction

```text
opportunity_scout.py
    |-- scout_bounties.py
    |-- strategic_claims.py
    |-- strategic_competition.py
    |-- strategic_readiness.py

strategic_competition.py --> scout_bounties.py
strategic_competition.py --> strategic_claims.py
strategic_competition.py  -X-> opportunity_scout.py
strategic_readiness.py --> strategic_claims.py
strategic_readiness.py  -X-> opportunity_scout.py
strategic_claims.py     -X-> opportunity_scout.py
scout_bounties.py       -X-> opportunity_scout.py
```

New leaf modules should follow the same rule. The orchestration layer may compose domain modules; domain modules should not reach back into the orchestrator.

## Refactoring direction

Do not perform a big-bang package rewrite. Extract one stable responsibility at a time, keep behavior green, then update this document and the generated map.

Likely future boundaries, when the code pressure justifies them:

- discovery/source adapters
- scoring and effort estimation
- reporting/notification formatting
- GitHub HTTP access and cache behavior

These are direction markers, not a requirement to create empty abstractions early.

## Invariants

- Paid candidates require explicit payment evidence.
- Strategic candidates must be refreshed and re-verified before they reach the final queue.
- Assigned, actively claimed, superseded, discussion-only, or already-implemented work must not be presented as ready work.
- Scanner misses are useful product feedback; audit paths should remain observable.
- Repository/network failures should degrade coverage explicitly rather than silently turning into positive verification.
- Tests must cover scanner policy without live network access.
- Ruff, strict mypy, and 100% statement + branch coverage are repository-wide quality gates.

## Documentation maintenance

- `AGENTS.md` owns coding-agent and contributor implementation rules.
- `ARCHITECTURE.md` owns boundaries, flow, and invariants.
- `CODEBASE_MAP.md` is generated from the AST and owns symbol navigation.
- `README.md` stays user-facing and should not become an internal design dump.
