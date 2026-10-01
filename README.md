# OSS Opportunity Scout

A lightweight GitHub scanner for finding open-source work worth doing, across two lanes:

- **Cash now:** explicit paid bounties and sponsored issues.
- **Career value:** bounded, mergeable issues in respected infrastructure/backend repositories.

The scout runs hourly, ranks new opportunities, creates a GitHub issue report, and only marks reported items as seen after at least one notification channel succeeds.

## Architecture

The fork-specific change is intentionally small:

- `scout_bounties.py` — upstream-compatible paid bounty discovery and strict payment/competition filters.
- `opportunity_scout.py` — dual-lane discovery, scoring, effort estimation, target-repo bonuses, verification, and ranked reporting.
- `seen_bounties.json` — shared notification state.
- `.github/workflows/bounty-scout.yml` — hourly runner.

Keeping the original paid scanner intact makes future upstream merges much less conflict-prone.

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

Strategic discovery starts with a curated target list covering AWS/Kubernetes, container runtimes, networking, RPC/storage, observability, Go tooling, Terraform, GitOps, and selected JS/TS infrastructure projects, plus narrow global searches for contributor-ready bugs.

Candidates are provisionally ranked first. Search depth is intentionally broader than the final queue, and only the strongest candidates get the expensive verification pass, which refreshes the source issue and checks:

- issue is still open and unassigned
- no obvious active claim
- no open implementation PR
- repository is available and not archived
- contribution guide at common repository locations

Career score considers repository reputation/activity, target-repo bonus, language/domain fit, technical depth, tests/contributor signals, scope, effort, and competition.

## Ranked output

Each reported candidate includes:

- repository, issue number, title, and URL
- paid/unpaid and reward
- payment confidence
- cash score /100
- career score /100
- effort: `<1h`, `1–3h`, `3–6h`, or `1d+`
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
python -m pip install ruff==0.16.9 mypy coverage
ruff format .
ruff check .
python -m py_compile scout_bounties.py opportunity_scout.py
mypy
coverage run --branch -m unittest -v
coverage report
GITHUB_TOKEN=... GITHUB_REPOSITORY=laclance/BountyScout python opportunity_scout.py
```

Ruff is the canonical Python formatter. Branch pushes in this repository are auto-formatted by GitHub Actions; pull requests also verify that committed Python is already Ruff-formatted.

Optional notification secrets remain supported:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DISCORD_WEBHOOK_URL`
