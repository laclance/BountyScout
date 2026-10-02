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

Evolve seen-state deliberately rather than treating it as an unbounded permanent list:

- introduce a versioned seen-state schema
- migrate backward-compatibly from the current JSON list
- keep `scout-state` as authoritative scheduled-workflow persistence
- add bounded revalidation and compaction
- avoid blind TTL expiration
- prune only with safe lifecycle evidence
- allow reopened opportunities to become eligible again where appropriate
- clean up legacy generated scan reports from before the current auto-close lifecycle

The package/domain model should remain unaware of Git branches. Local execution should
continue to treat the state file as ordinary local data.

## Phase 4C — app decomposition

Further split cohesive orchestration boundaries out of `bountyscout.app` where that
improves readability and testability. Likely extraction areas include:

- strategic discovery orchestration
- strategic verification orchestration
- report delivery
- state commit/maintenance orchestration

Prefer concrete cohesive boundaries. Do not introduce generic service layers or a
dependency-injection framework merely to reduce file size.

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
