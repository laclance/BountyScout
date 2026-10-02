from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping, cast

from bountyscout import (
    delivery,
    github as shared_github,
    paid,
    paid_verification,
    scoring,
    state,
)
from bountyscout.types import GitHubIssue

# Configuration
MAX_COMMENTS = paid.MAX_COMMENTS  # Compatibility alias

# GitHub search queries for active bounty opportunities
SEARCH_QUERIES = [
    "is:issue is:open bounty in:title,body sort:updated-desc",
    "is:issue is:open reward bounty sort:updated-desc",
    'is:issue is:open "paid" "PR" "bounty" sort:updated-desc',
    'is:issue is:open "Opire" bounty sort:updated-desc',
]

# Strong payment signals. A plain mention of "bounty", "paid", or "reward" is
# deliberately not enough because those words appear in unrelated issues.
PAYMENT_TERM_RE = paid.PAYMENT_TERM_RE
AMOUNT_RE = paid.AMOUNT_RE

CLAIM_PATTERNS = paid_verification.CLAIM_PATTERNS
UNFUNDED_PROPOSAL_PATTERNS = paid_verification.UNFUNDED_PROPOSAL_PATTERNS
META_ALERT_MARKERS = paid_verification.META_ALERT_MARKERS


def github_get(url: str, token: str | None = None, timeout: int = 20) -> Any:
    """Compatibility wrapper for the historical paid GitHub transport."""
    return shared_github.paid_github_get(url, token, timeout)


def search_github(query: str, token: str | None = None, per_page: int = 15) -> dict[str, Any]:
    """Compatibility wrapper for canonical GitHub Issues Search transport."""
    return cast(
        dict[str, Any],
        shared_github.search_github(query, token, per_page, fetch_json=github_get),
    )


def payment_signal(item: Mapping[str, Any]) -> str | None:
    """Compatibility wrapper for canonical paid payment-signal policy."""
    return paid.payment_signal(cast(GitHubIssue, item))


def issue_repo_and_number(item: Mapping[str, Any]) -> tuple[str | None, int | None]:
    """Extract owner/repo and issue number from a GitHub issue search item."""
    url = str(item.get("html_url", ""))
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)", url)
    if not match:
        return None, None
    return match.group(1), int(match.group(2))


def has_existing_implementation_pr(repo: str, issue_number: int, token: str | None) -> str | None:
    """Compatibility wrapper for canonical paid implementation-PR verification."""
    return paid_verification.has_existing_implementation_pr(
        repo,
        issue_number,
        token,
        fetch_json=github_get,
    )


def active_claim_reason(
    repo: str, issue_number: int, comments_count: int, token: str | None
) -> str | None:
    """Compatibility wrapper for canonical paid active-claim verification."""
    return paid_verification.active_claim_reason(
        repo,
        issue_number,
        comments_count,
        token,
        fetch_json=github_get,
    )


def is_clean_candidate(item: Mapping[str, Any]) -> bool:
    """Compatibility wrapper for canonical paid eligibility policy."""
    return paid.is_clean_candidate(cast(GitHubIssue, item))


def candidate_rejection_reason(
    item: Mapping[str, Any], token: str | None
) -> tuple[str | None, str | None]:
    """Compatibility wrapper for canonical paid rejection verification."""
    return paid_verification.candidate_rejection_reason(
        cast(GitHubIssue, item),
        token,
        existing_pr_checker=has_existing_implementation_pr,
        active_claim_checker=active_claim_reason,
    )


def parse_github_datetime(value: Any) -> datetime | None:
    """Parse a GitHub ISO timestamp, returning None when unavailable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def usd_like_amount_from_signal(signal: str | None) -> float | None:
    """Compatibility wrapper for canonical package reward parsing."""
    return scoring.usd_like_amount_from_signal(signal)


def fetch_repo_metadata(repo: str, token: str | None) -> dict[str, Any]:
    """Fetch lightweight repository metadata used only for ranking."""
    data = github_get(f"https://api.github.com/repos/{repo}", token)
    return data if isinstance(data, dict) else {}


def score_candidate(
    item: Mapping[str, Any], signal: str, repo_meta: Mapping[str, Any]
) -> tuple[int, str, list[str]]:
    """Return a transparent 0-100 triage score and short reasons."""
    score = 0
    reasons = []

    # Payment confidence. All candidates already passed the payment filter;
    # this only ranks stronger evidence above weaker evidence.
    if signal.startswith("explicit bounty command"):
        score += 25
        reasons.append("explicit bounty command")
    elif signal.startswith("bounty labels"):
        score += 22
        reasons.append("amount-bearing bounty labels")
    elif signal.startswith("named bounty platform"):
        score += 20
        reasons.append("named bounty platform")
    else:
        score += 16
        reasons.append("explicit payment wording")

    # Reward size matters, but only modestly: a tiny task can still be useful.
    amount = usd_like_amount_from_signal(signal)
    if amount is not None:
        if amount >= 100:
            score += 14
            reasons.append("$100+ stated reward")
        elif amount >= 25:
            score += 10
            reasons.append("$25+ stated reward")
        elif amount >= 5:
            score += 6
            reasons.append("$5+ stated reward")
        elif amount > 0:
            score += 3
            reasons.append("micro-bounty")

    # Prefer established, active repositories without making stars mandatory.
    stars = int(repo_meta.get("stargazers_count") or 0)
    if stars >= 1000:
        score += 15
        reasons.append("established repo")
    elif stars >= 100:
        score += 10
        reasons.append("100+ repo stars")
    elif stars >= 10:
        score += 5
        reasons.append("10+ repo stars")

    pushed_at = parse_github_datetime(repo_meta.get("pushed_at"))
    if pushed_at:
        repo_age_days = (datetime.now(timezone.utc) - pushed_at).days
        if repo_age_days <= 30:
            score += 10
            reasons.append("repo active in last 30d")
        elif repo_age_days <= 90:
            score += 5
            reasons.append("repo active in last 90d")

    if repo_meta.get("archived"):
        score -= 30
        reasons.append("archived repo")

    # Fresh, quiet issues are less likely to have hidden competition.
    comments = int(item.get("comments") or 0)
    if comments == 0:
        score += 8
        reasons.append("zero comments")
    elif comments <= 2:
        score += 5
        reasons.append("very low comment count")
    elif comments <= 5:
        score += 2

    created_at = parse_github_datetime(item.get("created_at"))
    if created_at:
        issue_age_days = (datetime.now(timezone.utc) - created_at).days
        if issue_age_days <= 7:
            score += 8
            reasons.append("fresh issue")
        elif issue_age_days <= 30:
            score += 4
            reasons.append("recent issue")

    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    text = f"{title}\n{body}".lower()
    labels = item.get("labels") or []
    label_names = " ".join(
        str(label.get("name", "")) if isinstance(label, dict) else str(label) for label in labels
    ).lower()

    if any(term in label_names for term in ("good first issue", "help wanted")):
        score += 5
        reasons.append("contributor-friendly label")

    if re.search(r"\b(?:test|tests|regression)\b", text):
        score += 5
        reasons.append("test/regression scope")

    if re.search(
        r"\b(?:one|single|small|narrow|deterministic|regression|fix)\b",
        title.lower(),
    ):
        score += 5
        reasons.append("apparently bounded scope")

    if re.search(
        r"\b(?:epic|roadmap|multi-phase|full system|architecture overhaul|rewrite)\b",
        text,
    ):
        score -= 10
        reasons.append("broad-scope wording")

    score = max(0, min(100, score))
    if score >= 70:
        tier = "strong"
    elif score >= 50:
        tier = "promising"
    else:
        tier = "low-confidence"

    return score, tier, reasons[:6]


def send_telegram_notification(token: str, chat_id: str, message: str) -> bool:
    """Compatibility wrapper for package delivery transport."""
    return delivery.send_telegram_notification(token, chat_id, message)


def send_discord_notification(webhook_url: str, message: str) -> bool:
    """Compatibility wrapper for package delivery transport."""
    return delivery.send_discord_notification(webhook_url, message)


def create_github_issue(repo_fullname: str, token: str, title: str, body: str) -> bool:
    """Compatibility wrapper for package delivery transport."""
    return delivery.create_github_issue(repo_fullname, token, title, body)


def main() -> None:
    # Load credentials/secrets from environment variables.
    github_token = os.environ.get("GITHUB_TOKEN")
    repo_fullname = os.environ.get("GITHUB_REPOSITORY")

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL")

    seen_state = state.load_seen_state()
    seen_urls = seen_state.urls()
    new_bounties: list[dict[str, Any]] = []
    new_bounty_urls: set[str] = set()
    rejected: dict[str, int] = {}
    repo_metadata_cache: dict[str, dict[str, Any]] = {}

    print("Scouting GitHub for active bounties...")
    for query in SEARCH_QUERIES:
        results = search_github(query, github_token)
        for item in results.get("items", []):
            url = item.get("html_url")
            if not url or url in seen_urls or url in new_bounty_urls:
                continue
            if not is_clean_candidate(item):
                continue

            rejection, signal = candidate_rejection_reason(item, github_token)
            if rejection:
                rejected[rejection] = rejected.get(rejection, 0) + 1
                print(f"Skipping {url}: {rejection}")
                continue

            repo, _ = issue_repo_and_number(item)
            assert repo is not None
            assert signal is not None
            if repo not in repo_metadata_cache:
                repo_metadata_cache[repo] = fetch_repo_metadata(repo, github_token)
            repo_meta = repo_metadata_cache[repo]

            score, tier, score_reasons = score_candidate(item, signal, repo_meta)
            new_bounties.append(
                {
                    "title": item.get("title"),
                    "url": url,
                    "repo": repo,
                    "comments": item.get("comments"),
                    "updated_at": item.get("updated_at"),
                    "payment_signal": signal,
                    "score": score,
                    "tier": tier,
                    "score_reasons": score_reasons,
                    "stars": int(repo_meta.get("stargazers_count") or 0),
                }
            )
            new_bounty_urls.add(url)

    if rejected:
        summary = ", ".join(f"{reason}={count}" for reason, count in sorted(rejected.items()))
        print(f"Filtered candidates: {summary}")

    new_bounties.sort(
        key=lambda bounty: (bounty["score"], -int(bounty["comments"] or 0)),
        reverse=True,
    )

    if not new_bounties:
        print("No new clean paid bounty opportunities found.")
        scan_time = datetime.now(timezone.utc)
        maintenance = state.maintain_seen_state(
            seen_state,
            scan_time,
            lambda url: shared_github.issue_lifecycle(url, github_token).status,
        )
        if maintenance.checked_urls:
            try:
                state.save_seen_state(maintenance.state)
            except state.SeenStateSaveError as exc:
                print(f"Error saving state file: {exc}")
        return

    print(f"Discovered {len(new_bounties)} NEW clean paid bounty opportunities!")

    scan_time = datetime.now(timezone.utc)
    now_str = scan_time.strftime("%Y-%m-%d %H:%M UTC")

    notif_lines = [
        f"🎯 *New Bounty Alert* ({now_str})",
        f"Found {len(new_bounties)} clean paid opportunity{'ies' if len(new_bounties) > 1 else ''}:\n",
    ]
    for idx, bounty in enumerate(new_bounties, start=1):
        notif_lines.append(f"{idx}. *{bounty['title']}*")
        notif_lines.append(f"   • Repository: `{bounty['repo']}`")
        notif_lines.append(f"   • Scout score: {bounty['score']}/100 ({bounty['tier']})")
        notif_lines.append(f"   • Payment signal: {bounty['payment_signal']}")
        notif_lines.append(f"   • Repo stars: {bounty['stars']}")
        notif_lines.append(f"   • Why: {', '.join(bounty['score_reasons'])}")
        notif_lines.append(f"   • Comments: {bounty['comments']}")
        notif_lines.append(f"   • Link: {bounty['url']}\n")

    notification_msg = "\n".join(notif_lines)

    # Only mark bounties as seen after at least one configured notification
    # channel confirms delivery.
    notification_attempted = False
    notification_succeeded = False

    if telegram_token and telegram_chat_id:
        notification_attempted = True
        notification_succeeded = (
            send_telegram_notification(
                telegram_token,
                telegram_chat_id,
                notification_msg,
            )
            or notification_succeeded
        )

    if discord_webhook:
        notification_attempted = True
        discord_msg = notification_msg.replace("•", "-")
        notification_succeeded = (
            send_discord_notification(discord_webhook, discord_msg) or notification_succeeded
        )

    if github_token and repo_fullname:
        notification_attempted = True
        issue_title = (
            f"🎯 Bounty Alert: {len(new_bounties)} Clean Paid "
            f"Opportunity{'ies' if len(new_bounties) > 1 else ''} found"
        )
        issue_body = (
            f"### Clean Paid Bounty Scan Results\n\n"
            f"**Scan Time:** {now_str}\n\n"
            "Filtered for explicit payment, no current assignee, no obvious "
            "active claim, and no open implementation PR found. Results are "
            "sorted by a transparent triage score.\n\n"
        )
        for idx, bounty in enumerate(new_bounties, start=1):
            issue_body += (
                f"#### {idx}. [{bounty['title']}]({bounty['url']})\n"
                f"- **Repository:** [{bounty['repo']}](https://github.com/{bounty['repo']})\n"
                f"- **Scout score:** {bounty['score']}/100 ({bounty['tier']})\n"
                f"- **Payment signal:** {bounty['payment_signal']}\n"
                f"- **Repo stars:** {bounty['stars']}\n"
                f"- **Why:** {', '.join(bounty['score_reasons'])}\n"
                f"- **Comments:** {bounty['comments']}\n"
                f"- **Last Updated:** {bounty['updated_at']}\n\n"
            )

        notification_succeeded = (
            create_github_issue(
                repo_fullname,
                github_token,
                issue_title,
                issue_body,
            )
            or notification_succeeded
        )

    if notification_attempted and notification_succeeded:
        maintenance = state.maintain_seen_state(
            seen_state,
            scan_time,
            lambda url: shared_github.issue_lifecycle(url, github_token).status,
        )
        next_state = maintenance.state
        next_state.mark_reported_many(
            new_bounty_urls,
            reported_at=scan_time.isoformat().replace("+00:00", "Z"),
        )
        try:
            state.save_seen_state(next_state)
        except state.SeenStateSaveError as exc:
            print(f"Error saving state file: {exc}")
        else:
            print("State saved successfully.")
    else:
        print("No notification was delivered; state not updated so these bounties will be retried.")


if __name__ == "__main__":
    main()
