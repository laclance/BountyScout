# Roadmap

This file describes deliberate future improvements. It is not a description of the
current runtime architecture.

- `ARCHITECTURE.md` describes what exists today.
- `ROADMAP.md` describes planned work and sequencing.
- `CODEBASE_MAP.md` is generated symbol navigation.
- `AGENTS.md` contains implementation and contributor rules.

## Product direction

Both scanner entry points remain supported product capabilities:

```bash
python opportunity_scout.py
python scout_bounties.py
```

The repository may eventually stop being maintained as a fork of upstream
BountyScout. Future work should therefore move reusable behavior toward canonical
package ownership without making standalone paid-only execution depend on the
combined opportunity-scout orchestrator.

## Phase 4A — strong domain typing

Keep raw transport/API data dynamic until it is validated, then use explicit internal
domain records through policy, scoring, reporting, and orchestration. Preserve current
dictionary runtime shapes during this phase; prefer standard-library typing tools such
as `TypedDict`, `Literal`, and `TypeAlias` over runtime model dependencies.

Broad result-object conversion is intentionally deferred.

## Phase 4B — state lifecycle

### Phase 4B.1 — canonical versioned state — complete

- `bountyscout.state` owns typed seen-state parsing, logical access, mutation, and persistence
- schema version 2 stores per-URL lifecycle fields without inventing legacy timestamps
- the historical JSON URL list remains loadable and migrates on the next successful save
- malformed, unreadable, or unsupported existing state fails closed instead of becoming empty
- `scout-state` remains authoritative scheduled-workflow persistence
- Python remains branch-agnostic and local execution still uses ordinary `seen_bounties.json`

### Phase 4B.2 — bounded retention and compaction — complete

- bounded direct issue revalidation uses a 20-call maximum per successful run
- entries are rechecked no more often than every 30 days, with unknown legacy timestamps treated as oldest
- deterministic ordering walks never-checked entries first, then oldest checked entries, then URL
- only confirmed closed GitHub issues are pruned; failures, 404s, and non-GitHub URLs remain seen
- complete quiet runs can compact state, while failed delivery or incomplete combined coverage persists no maintenance changes
- confirmed-closed entries may surface again if the issue is later reopened

### Phase 4B.3 — legacy generated-report cleanup — complete

- one-time cleanup requires explicit repository, open-state, non-PR, label, title/body, and automation-author identity signals
- the 28 historical pre-auto-close combined queue reports were closed as `not_planned`; paid-only alert artifacts were intentionally left untouched
- current generated reports remain owned by the normal auto-close delivery lifecycle; no recurring cleanup service exists

## Phase 4C — app decomposition

Further split cohesive orchestration boundaries out of `bountyscout.app` where that
improves readability and testability.

### Phase 4C.1 — strategic discovery orchestration — complete

- `bountyscout.strategic.discovery` owns strategic source-pool collection, near-miss auditing, adaptive inspection selection, and deterministic pre-verification ranking
- `bountyscout.app` retains compatibility wrappers and passes narrow paid-compatible callbacks rather than spreading the root `scout_bounties.py` dependency
- discovery queries, request budgets, ordering, and cache lifetime remain unchanged

### Phase 4C.2 — strategic verification orchestration — complete

- `bountyscout.strategic.verification` owns ranked deep-verification orchestration, bounded per-repository settlement, source-failure handling, and final strategic selection
- `bountyscout.app.verify()` remains the transitional mixed paid/strategic compatibility edge and is supplied as a narrow callback
- verification request budgets, source-failure semantics, upper-bound pruning, repo-slot settlement, and final per-repo ordering remain unchanged

### Phase 4C.3 — combined run lifecycle — complete

- `bountyscout.run` owns combined discovery coordination, final queue assembly, coverage accounting, delivery aggregation, and the transactional seen-state commit
- `bountyscout.app.main()` is now a thin environment/callback assembly layer while remaining the transitional root paid-scanner compatibility edge
- delivery/state semantics, Search budgets, cache lifetime, report lifecycle, and standalone paid-scanner behavior remain unchanged
- root paid-scanner operations enter the run layer only through narrow typed callbacks

**Phase 4C is complete.** The remaining `bountyscout.app` responsibilities are intentional compatibility/application seams rather than another decomposition target.

## Phase 4D — effort estimator decomposition

Decompose `estimate_effort_details()` into smaller behavior-preserving helpers.

The refactor must preserve:

- regex semantics
- rule precedence
- emitted reasons
- effort buckets

Any scoring behavior change discovered during decomposition belongs in a separate
regression-backed change.

## Phase 4E — paid-scanner independence

Gradually reduce root `scout_bounties.py` ownership of reusable package behavior.
The long-term direction may become:

```text
scout_bounties.py
    ↓
canonical package-owned paid scanner
```

The command `python scout_bounties.py` must remain fully supported throughout. Do
not make the paid-only entry point depend on combined application orchestration.

## Deferred result-object work

After mapping-based domain contracts are stable, consider immutable result objects or
dataclasses where they improve invariants for:

- verification outcomes
- scoring results
- effort results
- source fetch results
- rejection results

Do not convert mapping-heavy runtime paths merely for stylistic consistency.

## Deferred state-file relocation

Do not move `seen_bounties.json` into a data/state directory until the state schema,
migration, retention, and compaction semantics are stable.

Scheduled production persistence currently uses the `scout-state` branch; relocation
must account for that workflow explicitly.

## Deferred architecture enforcement

If dependency-direction drift becomes recurring, consider lightweight CI checks for
invariants such as:

- strategic policy must not import `bountyscout.app`
- package/domain modules must not import root `opportunity_scout.py`
- transport must not depend on high-level scanner policy

Prefer simple checks with clear maintenance value over custom architecture tooling.
