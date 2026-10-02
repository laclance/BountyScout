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
| `opportunity_scout.py` | Stable executable entry point that calls `bountyscout.app.main()` | Root shim only; no scanner policy or compatibility façade |
| `bountyscout/app.py` | Combined application orchestration plus strategic discovery, verification, and delivery | Canonical fork-owned application implementation |
| `bountyscout/github.py` | Fork-owned GitHub JSON transport, generic issue/timestamp parsing, and keyed per-scan cache-fill primitives | No scanner policy; source refreshes stay uncached while stable repo/guide lookups may be cached |
| `bountyscout/types.py` | Canonical static domain literals and mapping records shared across package-owned scanner code | Dependency-light typing vocabulary only; raw external JSON remains dynamic until validated |
| `bountyscout/reporting.py` | GitHub queue reports, compact reject/audit summaries, and length-safe notification rendering | Presentation-only; no network I/O or scanner policy decisions |
| `bountyscout/scoring.py` | Pure-ish effort estimation plus cash/career ranking over already-fetched evidence | No network I/O; owns scoring math and effort calibration, including trusted maintainer-history signals |
| `bountyscout/sources.py` | Curated GitHub issue pools, issue/comment fetches, contribution-guide lookup, bounty-platform adapters, and bounded adaptive inspection selection | Owns external source retrieval/parsing; does not rank final candidates or decide readiness |
| `bountyscout/strategic/claims.py` | Pure first-person ownership / implementation / PR-intent language detection | No network I/O and no dependency on `opportunity_scout.py` |
| `bountyscout/strategic/competition.py` | Active-claim, linked/timeline implementation-PR detection, and competition precedence | Uses local evidence before timeline I/O; never imports `opportunity_scout.py` |
| `bountyscout/strategic/readiness.py` | Pure maintainer-readiness, triage, lifecycle, dashboard, and release-tracking policy | Interprets issue/comment evidence only; no network I/O or dependency on `opportunity_scout.py` |
| `seen_bounties.json` | Local runtime seen-state file | Version 2 is canonical; legacy URL lists load losslessly and rewrite as version 2 on the next successful save |
| `.github/workflows/bounty-scout.yml` | Scheduled scanner execution | Runtime workflow, not the quality gate |
| `.github/workflows/python-quality.yml` | Formatting, lint, map, typing, tests, coverage | Must stay fast enough for normal PR iteration |

## Dependency direction

```text
opportunity_scout.py --> bountyscout.app

bountyscout.app
    |-- bountyscout.github
    |-- scout_bounties.py
    |-- bountyscout.reporting
    |-- bountyscout.scoring
    |-- bountyscout.sources
    |-- bountyscout.strategic.claims
    |-- bountyscout.strategic.competition
    |-- bountyscout.strategic.readiness

bountyscout.types -X-> package policy / orchestration modules
bountyscout.github -X-> scout_bounties.py
bountyscout.sources --> bountyscout.github
bountyscout.scoring --> bountyscout.github
bountyscout.scoring --> scout_bounties.py  # payment-specific helper only
bountyscout.scoring --> bountyscout.strategic.readiness
bountyscout.strategic.competition --> bountyscout.github
bountyscout.strategic.competition --> scout_bounties.py  # shared paid-compatible policy only
bountyscout.strategic.competition --> bountyscout.strategic.claims
bountyscout.strategic.readiness --> bountyscout.strategic.claims

package/domain modules -X-> opportunity_scout.py
package/domain modules -X-> bountyscout.app
scout_bounties.py       -X-> opportunity_scout.py
```

New leaf modules should follow the same rule. The orchestration layer may compose domain modules; domain modules should not reach back into the orchestrator. `bountyscout.types` is deliberately dependency-light so policy, scoring, reporting, and orchestration can share domain contracts without creating circular imports.

## Refactoring direction

Phase 3B makes generic GitHub issue parsing and timestamp parsing fork-owned in `bountyscout.github`. `scout_bounties.py` intentionally retains behavior-equivalent copies so the paid scanner remains standalone and upstream-compatible. This small duplication is deliberate: package imports of `scout_bounties` now mark genuine paid-scanner behavior or deliberately shared compatibility policy rather than generic GitHub/data utilities.

## Invariants

- Paid candidates require explicit payment evidence and must represent open work rather than payout-history/leaderboard summaries.
- Strategic candidates must be refreshed and re-verified before they reach the final queue.
- Assigned, actively claimed, superseded, discussion-only, or already-implemented work must not be presented as ready work.
- Scanner misses are useful product feedback; audit paths should remain observable.
- Repository/network failures should degrade coverage explicitly rather than silently turning into positive verification.
- Strategic deep verification is rate-budgeted: inspect broadly, verify in rank order, and stop only when remaining candidates cannot displace the kept set under the known verification-score uplift bound. Final strategic effort may use the already-fetched trusted maintainer discussion to recognize implementation-history complexity; preview scoring stays source-only and this calibration must not add network fan-out.
- Comment-fetch failure must remain distinguishable from a real empty discussion thread; strategic verification fetches each issue comment thread once through the checked path and reuses that evidence for payment detection, readiness, competition, and final scoring. Failed implementation-PR timeline checks are also verification failures rather than evidence of no competition. Strategic verification does not use per-issue GitHub Search queries, preserving Search quota for discovery. Incomplete verification runs warn prominently and do not advance seen-state.
- Tests must cover scanner policy without live network access.
- Ruff, strict mypy, and 100% statement + branch coverage are repository-wide quality gates.

## Documentation maintenance

- `AGENTS.md` owns coding-agent and contributor implementation rules.
- `ARCHITECTURE.md` owns boundaries, flow, and invariants.
- `CODEBASE_MAP.md` is generated from the AST and owns symbol navigation.
- `README.md` stays user-facing and should not become an internal design dump.
