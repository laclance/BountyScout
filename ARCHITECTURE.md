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
| `scout_bounties.py` | Upstream-compatible paid discovery, broader paid rejection/competition checks, notifications, and standalone entry point | Delegates pure paid eligibility/payment-signal policy to `bountyscout.paid`; keep remaining changes conservative to reduce upstream merge conflicts |
| `opportunity_scout.py` | Stable executable entry point that calls `bountyscout.app.main()` | Root shim only; no scanner policy or compatibility façade |
| `bountyscout/app.py` | Executable/application assembly, environment wiring, mixed paid/strategic `verify()` compatibility edge, and root paid-scanner adapters | Keeps the transitional root `scout_bounties.py` dependency localized and supplies narrow callbacks to package orchestration |
| `bountyscout/run.py` | Combined scan lifecycle, queue assembly, coverage accounting, delivery aggregation, and transactional seen-state commit | Owns one combined run without importing `scout_bounties.py`, `bountyscout.app`, or `opportunity_scout.py`; root transports enter only through typed callbacks |
| `bountyscout/github.py` | Fork-owned GitHub JSON transport, generic issue/timestamp parsing, and keyed per-scan cache-fill primitives | No scanner policy; source refreshes stay uncached while stable repo/guide lookups may be cached |
| `bountyscout/paid.py` | Pure paid-opportunity basic eligibility and issue-level payment-signal recognition over already-fetched issue evidence | No network I/O; canonical owner of `MAX_COMMENTS`, `PAYMENT_TERM_RE`, `AMOUNT_RE`, `payment_signal()`, and `is_clean_candidate()` |
| `bountyscout/types.py` | Canonical static domain literals and mapping records shared across package-owned scanner code | Dependency-light typing vocabulary only; raw external JSON remains dynamic until validated |
| `bountyscout/state.py` | Canonical typed, versioned seen-state parsing, legacy migration, logical membership/mutation, and deterministic atomic persistence | Local-file state only; branch-agnostic and fail-closed for malformed or unsupported existing state |
| `bountyscout/reporting.py` | GitHub queue reports, compact reject/audit summaries, and length-safe notification rendering | Presentation-only; no network I/O or scanner policy decisions |
| `bountyscout/scoring.py` | Pure-ish effort estimation plus cash/career ranking over already-fetched evidence | No network I/O; owns scoring math and effort calibration, including trusted maintainer-history signals |
| `bountyscout/sources.py` | Curated GitHub issue pools, issue/comment fetches, contribution-guide lookup, bounty-platform adapters, and bounded adaptive inspection selection | Owns external source retrieval/parsing; does not rank final candidates or decide readiness |
| `bountyscout/strategic/claims.py` | Pure first-person ownership / implementation / PR-intent language detection | No network I/O and no dependency on `opportunity_scout.py` |
| `bountyscout/strategic/competition.py` | Active-claim, linked/timeline implementation-PR detection, and competition precedence | Uses local evidence before timeline I/O; never imports `opportunity_scout.py` |
| `bountyscout/strategic/discovery.py` | Strategic source-pool collection, near-miss audit diagnostics, adaptive inspection selection, and deterministic pre-verification ranking | Accepts narrow app adapters for transitional paid-compatible predicates/signals; never imports root `scout_bounties.py` or `bountyscout.app` |
| `bountyscout/strategic/verification.py` | Ranked strategic deep-verification orchestration, bounded per-repo settlement, source-failure handling, and final strategic selection | Accepts typed app callbacks for mixed verification/preflight behavior; never imports root `scout_bounties.py` or `bountyscout.app` |
| `bountyscout/strategic/readiness.py` | Pure maintainer-readiness, triage, lifecycle, dashboard, and release-tracking policy | Interprets issue/comment evidence only; no network I/O or dependency on `opportunity_scout.py` |
| `seen_bounties.json` | Local runtime seen-state file | Version 2 is canonical; legacy URL lists load losslessly and rewrite as version 2 on the next successful save |
| `.github/workflows/bounty-scout.yml` | Scheduled scanner execution | Runtime workflow, not the quality gate |
| `.github/workflows/python-quality.yml` | Formatting, lint, map, typing, tests, coverage | Must stay fast enough for normal PR iteration |

## Dependency direction

```text
opportunity_scout.py --> bountyscout.app

bountyscout.app
    |-- bountyscout.github
    |-- bountyscout.paid
    |-- bountyscout.run
    |-- scout_bounties.py
    |-- bountyscout.reporting
    |-- bountyscout.scoring
    |-- bountyscout.sources
    |-- bountyscout.strategic.claims
    |-- bountyscout.strategic.competition
    |-- bountyscout.strategic.discovery
    |-- bountyscout.strategic.verification
    |-- bountyscout.strategic.readiness

bountyscout.run --> bountyscout.reporting
bountyscout.run --> bountyscout.state
bountyscout.run -X-> scout_bounties.py
bountyscout.run -X-> bountyscout.app
bountyscout.run -X-> opportunity_scout.py
bountyscout.types -X-> package policy / orchestration modules
bountyscout.github -X-> scout_bounties.py
bountyscout.paid -X-> scout_bounties.py
bountyscout.sources --> bountyscout.github
bountyscout.scoring --> bountyscout.github
bountyscout.scoring --> bountyscout.strategic.readiness
bountyscout.strategic.competition --> bountyscout.github
bountyscout.strategic.competition --> scout_bounties.py  # shared paid-compatible policy only
bountyscout.strategic.competition --> bountyscout.strategic.claims
bountyscout.strategic.discovery --> bountyscout.github
bountyscout.strategic.discovery --> bountyscout.scoring
bountyscout.strategic.discovery --> bountyscout.sources
bountyscout.strategic.discovery --> bountyscout.strategic.readiness
bountyscout.strategic.discovery -X-> scout_bounties.py
bountyscout.strategic.verification --> bountyscout.sources
bountyscout.strategic.verification --> bountyscout.strategic.discovery
bountyscout.strategic.verification -X-> scout_bounties.py
bountyscout.strategic.verification -X-> bountyscout.app
bountyscout.strategic.readiness --> bountyscout.strategic.claims

package/domain modules -X-> opportunity_scout.py
package/domain modules -X-> bountyscout.app
scout_bounties.py --> bountyscout.paid
scout_bounties.py --> bountyscout.state
scout_bounties.py       -X-> opportunity_scout.py
```

New leaf modules should follow the same rule. The orchestration layer may compose domain modules; domain modules should not reach back into the orchestrator. `bountyscout.types` is deliberately dependency-light so policy, scoring, reporting, and orchestration can share domain contracts without creating circular imports.

## Refactoring direction

Phase 3B makes generic GitHub issue parsing and timestamp parsing fork-owned in `bountyscout.github`. `scout_bounties.py` intentionally retains behavior-equivalent copies so the paid scanner remains standalone and upstream-compatible. This small duplication is deliberate: package imports of `scout_bounties` now mark genuine paid-scanner behavior or deliberately shared compatibility policy rather than generic GitHub/data utilities.

Historical generated queue reports from before the current auto-close lifecycle were cleaned once with `scripts/close_legacy_scan_reports.py` after explicit report-identity verification. Current generated reports are auto-closed during normal delivery, and there is no recurring cleanup service.

Phase 4C.1 moves strategic source collection, near-miss auditing, adaptive inspection, and pre-verification ranking into `bountyscout.strategic.discovery`. Phase 4C.2 moves ranked deep-verification orchestration, per-repository bounded settlement, source-failure handling, and final strategic selection into `bountyscout.strategic.verification`. Phase 4C.3 moves the combined discovery lifecycle, final queue assembly, coverage accounting, delivery aggregation, and seen-state transaction into `bountyscout.run`. `bountyscout.app` now stays at the executable/compatibility edge: it reads environment configuration and supplies narrow paid-scanner transport/policy callbacks without spreading the root `scout_bounties.py` dependency.

Phase 4E.1 moved USD-like reward parsing into `bountyscout.scoring`. Phase 4E.2 moves basic paid eligibility and issue-level payment-signal recognition into the network-free `bountyscout.paid` policy module. Root paid symbols remain compatibility aliases/wrappers, while `bountyscout.app` consumes the package policy directly; search transport and broader paid rejection/competition orchestration remain at the root boundary.

## Invariants

- Paid candidates require explicit payment evidence and must represent open work rather than payout-history/leaderboard summaries.
- Strategic candidates must be refreshed and re-verified before they reach the final queue.
- Assigned, actively claimed, superseded, discussion-only, or already-implemented work must not be presented as ready work.
- Scanner misses are useful product feedback; audit paths should remain observable.
- Repository/network failures should degrade coverage explicitly rather than silently turning into positive verification.
- Strategic deep verification is rate-budgeted: inspect broadly, verify in rank order, and stop only when remaining candidates cannot displace the kept set under the known verification-score uplift bound. Final strategic effort may use the already-fetched trusted maintainer discussion to recognize implementation-history complexity; preview scoring stays source-only and this calibration must not add network fan-out.
- Comment-fetch failure must remain distinguishable from a real empty discussion thread; strategic verification fetches each issue comment thread once through the checked path and reuses that evidence for payment detection, readiness, competition, and final scoring. Failed implementation-PR timeline checks are also verification failures rather than evidence of no competition. Strategic verification does not use per-issue GitHub Search queries, preserving Search quota for discovery. Incomplete verification runs warn prominently and do not advance seen-state.
- Seen-state loading fails closed for malformed JSON, malformed schema, unsupported versions, or unreadable existing files; only a genuinely absent file means empty first-run state. Legacy list entries remain logically seen and migrate with unknown lifecycle timestamps represented as `null`.
- Seen-state maintenance is bounded to 20 direct GitHub issue checks per successful run with a 30-day minimum recheck interval. Selection is deterministic: never-checked entries first, then oldest `last_checked_at`, then URL. `last_checked_at` records the maintenance attempt time, including not-found and failed checks, so one bad entry cannot monopolize later hourly batches.
- Only direct lifecycle evidence of `closed` prunes a GitHub issue. `open`, ambiguous `404`/not-found, auth/rate-limit/server/network failures, malformed responses, and checker exceptions all retain the URL. Non-GitHub URLs remain seen and are excluded from GitHub maintenance until a platform-specific lifecycle policy exists.
- Successful maintenance and newly reported URLs are persisted as one state snapshot. Complete quiet runs may persist maintenance alone; incomplete combined coverage or failed delivery persists neither maintenance nor newly reported URLs. A later reopen of a previously confirmed-closed issue is intentionally eligible to surface again.
- `bountyscout.state` knows only the local state file. Scheduled production persistence remains the workflow's `scout-state` responsibility, and Python state code contains no Git branch/worktree logic.
- Seen-state advances only after a configured delivery succeeds, and combined runs with incomplete discovery/verification coverage still do not advance it. A GitHub report whose auto-close step fails remains a failed delivery for this transaction.
- Tests must cover scanner policy without live network access.
- Ruff, strict mypy, and 100% statement + branch coverage are repository-wide quality gates.

## Documentation maintenance

- `AGENTS.md` owns coding-agent and contributor implementation rules.
- `ARCHITECTURE.md` owns boundaries, flow, and invariants.
- `CODEBASE_MAP.md` is generated from the AST and owns symbol navigation.
- `README.md` stays user-facing and should not become an internal design dump.
