# OSS Opportunity Scout

A lightweight GitHub scanner for finding open-source work worth doing, across two lanes:

- **Cash now:** explicit paid bounties and sponsored issues.
- **Career value:** bounded, mergeable issues in respected infrastructure/backend repositories.

The scout runs hourly, ranks new opportunities, creates a GitHub issue report, and only marks reported items as seen after at least one notification channel succeeds.

## Architecture

The paid scanner remains intentionally close to upstream, while fork-specific strategic behavior is being split into focused modules:

- `scout_bounties.py` — upstream-compatible paid bounty discovery and strict payment/competition filters.
- `opportunity_scout.py` — application orchestration and strategic scanner composition.
- `opportunity_scoring.py` — effort estimation and cash/career ranking over verified evidence.
- `strategic_claims.py` — pure contributor ownership/implementation claim detection.
- `strategic_competition.py` — active-claim and implementation-PR competition checks.
- `strategic_readiness.py` — pure maintainer-readiness and lifecycle policy.
- `seen_bounties.json` — shared notification state.
- `.github/workflows/bounty-scout.yml` — hourly runner.

See `ARCHITECTURE.md` for data flow and module boundaries, `AGENTS.md` for AI/human implementation rules, and `CODEBASE_MAP.md` for the generated structural index.

## Paid lane

Paid candidates reuse the existing BountyScout rules, including rejection of:

- pull requests, assigned issues, and overcrowded threads
- recursive BountyScout alerts and obvious spam
- unfunded bounty proposals
- meta/bug-bounty monitoring alerts
- issues without a real payment signal
- issues with an open implementation PR
- obvious active claim comments

Cash score considers payment confidence, stated reward, rough expected hourly value, competition, and repository legitimacy/activity.

## Strategic OSS lane

Strategic discovery starts with a curated target list covering AWS/Kubernetes, container runtimes, networking, RPC/storage, observability, Go tooling, Terraform, GitOps, and selected JS/TS infrastructure projects, plus narrow global searches for contributor-ready bugs. Curated repositories are read through GitHub's core Issues API rather than the Search API, so every target gets its own result budget without exhausting search-rate limits.

For each curated repository, the scout activity-inspects up to 15 plausible issues, re-ranks them using issue/comment freshness, and then fully verifies candidates in order until up to three valid opportunities survive. Search depth is intentionally broader than the final queue. Verification refreshes the source issue and checks:

- issue is still open and unassigned
- no obvious active claim
- no open implementation PR
- repository is available and not archived
- contribution guide at common repository locations

Career score considers repository reputation/activity, target-repo bonus, language/domain fit, technical depth, tests/contributor signals, scope, effort, competition, issue freshness, and recent maintainer/discussion activity. Old issues are penalized only when they are actually inactive rather than merely old.

The scout also emits potential scanner misses: strong-looking items that were filtered early, fell just outside a repo's inspection pool, or narrowly missed the quality floor. These are intended as feedback for tuning false-negative and false-positive behavior over time.

## Ranked output

Each reported candidate includes:

- repository, issue number, title, and URL
- paid/unpaid and reward
- payment confidence
- cash score /100
- career score /100
- effort: `<1h`, `1–3h`, `3–6h`, `6–12h`, or `1d+`, with a short effort basis
- competition: `none`, `low`, `medium`, or `high`
- stars, recent activity, language, and labels
- scoring reasons
- contribution-guide link when found

The GitHub issue report also includes examples rejected during final verification.

## Workflow

The intended human loop is:

```text
Scout finds candidates
       ↓
rank top candidates
       ↓
manually verify top 3–5
       ↓
choose 1–2
       ↓
inspect issue + CONTRIBUTING + PR competition
       ↓
implement → test/review → PR
       ↓
return to queue
```

Keep no more than two issues actively being implemented at once.

## Running

The GitHub Action runs hourly and can also be triggered manually from **Actions → OSS Opportunity Scout Hourly**.

Locally:

```bash
python -m pip install -r requirements-dev.txt
make quality
GITHUB_TOKEN=... GITHUB_REPOSITORY=laclance/BountyScout python opportunity_scout.py
```

Run `make format` after Python edits and `make map` when production modules or top-level symbols change. Ruff is the canonical Python formatter; strict mypy, the generated code map, and 100% statement + branch coverage are enforced in CI.

Optional notification secrets remain supported:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DISCORD_WEBHOOK_URL`
