from __future__ import annotations

import json
import os
import urllib.request
import urllib.parse
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

# Configuration
STATE_FILE = "seen_bounties.json"
MAX_COMMENTS = 25  # Filter out overcrowded threads

# GitHub search queries for active bounty opportunities
SEARCH_QUERIES = [
    "is:issue is:open bounty in:title,body sort:updated-desc",
    "is:issue is:open reward bounty sort:updated-desc",
    'is:issue is:open "paid" "PR" "bounty" sort:updated-desc',
    'is:issue is:open "Opire" bounty sort:updated-desc',
]

# Strong payment signals. A plain mention of "bounty", "paid", or "reward" is
# deliberately not enough because those words appear in unrelated issues.
PAYMENT_TERM_RE = r"(?:bounty|reward|payout|compensation|pay(?:ment|s|ing|s)?|paid)"
AMOUNT_RE = (
    r"(?:[$€£]\s*\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:usd|usdc|usdt|eur|gbp|xmr|sol|eth|btc)\b)"
)

CLAIM_PATTERNS = [
    r"/attempt\b",
    r"\bi(?:'d| would) like to (?:take|work on|handle|resolve)",
    r"\bi(?:'m| am) taking (?:this|an independent pass)",
    r"\bi(?:'m| am) working on (?:this|it)",
    r"\bplease assign(?: this issue)? to me\b",
    r"\bkindly assign(?: it| this issue)? to me\b",
    r"\bassign (?:this|it) to me\b",
    r"\bi can work on this\b",
    r"\bi have implemented\b",
    r"\bdelivered in pr\b",
    r"\bsubmitted (?:a )?pr\b",
]


UNFUNDED_PROPOSAL_PATTERNS = [
    r"\[bounty proposal\]",
    r"\bproposed amount, not an existing award\b",
    r"\$\s*\d[\d,]*(?:\.\d+)?\s+proposed\b",
    r"\bwould (?:a |an )?(?:us\$|\$)?\s*\d[\d,]*(?:\.\d+)? bounty be appropriate\b",
    r"\bpropos(?:e|ed|ing) (?:a )?(?:paid work|bounty)\b",
]

META_ALERT_MARKERS = [
    "new in-scope",
    "bug bounty program(s) added",
    "bounty-watch",
    "bounty watch",
]


def load_seen_bounties() -> set[str]:
    """Load previously seen bounty URLs from the state file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
        except Exception as e:
            print(f"Error loading state file: {e}")
    return set()


def save_seen_bounties(seen_urls: Iterable[str]) -> bool:
    """Save the updated list of seen bounty URLs."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen_urls), f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving state file: {e}")
        return False


def github_get(url: str, token: str | None = None, timeout: int = 20) -> Any:
    """Fetch JSON from the GitHub API."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "MyPersonalBountyScout",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"GitHub API Error for {url}: {e}")
        return None


def search_github(query: str, token: str | None = None, per_page: int = 15) -> dict[str, Any]:
    """Fetch search results from GitHub Issues API."""
    url = f"https://api.github.com/search/issues?{urllib.parse.urlencode({'q': query, 'per_page': per_page})}"
    data = github_get(url, token)
    return data if isinstance(data, dict) else {}


def payment_signal(item: Mapping[str, Any]) -> str | None:
    """Return a strong payment signal, or None when payment is not explicit."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = item.get("labels") or []
    label_names = [
        str(label.get("name", "")) if isinstance(label, dict) else str(label) for label in labels
    ]

    text = f"{title}\n{body}"
    lower = text.lower()
    labels_text = " ".join(label_names).lower()

    # Explicit bounty commands used by Algora and similar integrations.
    slash_match = re.search(r"/bounty\s+(" + AMOUNT_RE + r")", text, re.IGNORECASE)
    if slash_match:
        return f"explicit bounty command: {slash_match.group(1).strip()}"

    # Require a payment/reward term close to an actual amount/currency.
    near_amount = re.search(
        PAYMENT_TERM_RE + r".{0,80}?(" + AMOUNT_RE + r")",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if near_amount:
        return f"payment term + amount: {near_amount.group(1).strip()}"

    amount_near_term = re.search(
        r"(" + AMOUNT_RE + r").{0,80}?" + PAYMENT_TERM_RE,
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if amount_near_term:
        return f"amount + payment term: {amount_near_term.group(1).strip()}"

    # Some bounty systems put the amount in a label and "bounty" in another.
    amount = re.search(AMOUNT_RE, labels_text, re.IGNORECASE)
    if amount and re.search(r"\b(?:bounty|reward|payout)\b", labels_text, re.IGNORECASE):
        return f"bounty labels: {amount.group(0).strip()}"

    # Platform-backed language can be explicit even when the exact amount is
    # stored outside the issue body.
    if re.search(r"\b(?:algora|opire)\b", lower) and re.search(
        r"\b(?:funded|bounty|reward|payout)\b", lower
    ):
        return "named bounty platform + funding language"

    return None


def issue_repo_and_number(item: Mapping[str, Any]) -> tuple[str | None, int | None]:
    """Extract owner/repo and issue number from a GitHub issue search item."""
    url = str(item.get("html_url", ""))
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)", url)
    if not match:
        return None, None
    return match.group(1), int(match.group(2))


def has_existing_implementation_pr(repo: str, issue_number: int, token: str | None) -> str | None:
    """Return a reason when an open PR appears to implement the issue."""
    # Search the number broadly. Quoting a bare issue number can miss PR
    # references such as "Fixes #123" in GitHub's search index.
    query = f"repo:{repo} is:pr is:open {issue_number}"
    results = search_github(query, token, per_page=50)

    issue_ref = re.compile(
        rf"(?:#\s*{issue_number}\b|/issues/{issue_number}\b|issue\s+#?\s*{issue_number}\b)",
        re.IGNORECASE,
    )
    for pr in results.get("items", []):
        pr_text = f"{pr.get('title', '')}\n{pr.get('body', '')}"
        if issue_ref.search(pr_text):
            return f"existing open implementation PR: {pr.get('html_url')}"

    return None


def active_claim_reason(
    repo: str, issue_number: int, comments_count: int, token: str | None
) -> str | None:
    """Return a reason when recent comments clearly claim or implement the task."""
    if not comments_count:
        return None

    params = urllib.parse.urlencode(
        {
            "per_page": min(int(comments_count), 30),
            "sort": "created",
            "direction": "desc",
        }
    )
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments?{params}"
    comments = github_get(url, token)
    if not isinstance(comments, list):
        return None

    for comment in comments:
        body = str(comment.get("body", ""))
        for pattern in CLAIM_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                author = (comment.get("user") or {}).get("login", "someone")
                return f"active claim by @{author}"

    return None


def is_clean_candidate(item: Mapping[str, Any]) -> bool:
    """Basic triage before the more expensive payment/competition checks."""
    # Skip if already a Pull Request
    if "pull_request" in item:
        return False

    # Skip BountyScout-generated alert issues so scouts do not recursively
    # discover other scouts (or themselves) as bounty opportunities.
    url = str(item.get("html_url", "")).lower()
    if "/bountyscout/issues/" in url:
        return False

    title = str(item.get("title", "")).lower()
    body = str(item.get("body", "")).lower()
    generated_alert_markers = [
        "bounty alert:",
        "active bounty scan results",
        "new opportunities found",
        "new opportunityies found",
    ]
    if any(marker in title or marker in body for marker in generated_alert_markers):
        return False

    # Skip if already assigned.
    if item.get("assignees"):
        return False

    # Skip if thread is overcrowded.
    if int(item.get("comments", 0)) > MAX_COMMENTS:
        return False

    # Skip cryptocurrency/article writing/spam keywords.
    blocklist = [
        "airdrop",
        "referral",
        "casino",
        "gambling",
        "trading bot",
        "blog post",
        "article writing",
        "tutorial proposal",
        "content creator",
    ]
    if any(term in title or term in body for term in blocklist):
        return False

    return True


def candidate_rejection_reason(
    item: Mapping[str, Any], token: str | None
) -> tuple[str | None, str | None]:
    """Apply strict money + competition checks and return a rejection reason."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = item.get("labels") or []
    label_names = [
        str(label.get("name", "")) if isinstance(label, dict) else str(label) for label in labels
    ]
    combined = f"{title}\n{body}"
    lower_combined = combined.lower()
    lower_labels = " ".join(label_names).lower()

    if any(re.search(pattern, combined, re.IGNORECASE) for pattern in UNFUNDED_PROPOSAL_PATTERNS):
        return "unfunded bounty proposal, not an existing award", None

    if any(marker in lower_combined or marker in lower_labels for marker in META_ALERT_MARKERS):
        return "meta/monitoring alert, not a contributor task", None

    signal = payment_signal(item)
    if not signal:
        return "no explicit payment signal", None

    repo, issue_number = issue_repo_and_number(item)
    if not repo or not issue_number:
        return "could not identify repository/issue number", signal

    pr_reason = has_existing_implementation_pr(repo, issue_number, token)
    if pr_reason:
        return pr_reason, signal

    claim_reason = active_claim_reason(
        repo,
        issue_number,
        int(item.get("comments", 0)),
        token,
    )
    if claim_reason:
        return claim_reason, signal

    return None, signal


def parse_github_datetime(value: Any) -> datetime | None:
    """Parse a GitHub ISO timestamp, returning None when unavailable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def usd_like_amount_from_signal(signal: str | None) -> float | None:
    """Extract a USD-like amount from a payment signal when comparable."""
    if not signal:
        return None

    dollar = re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)", signal)
    if dollar:
        return float(dollar.group(1).replace(",", ""))

    currency = re.search(
        r"(\d[\d,]*(?:\.\d+)?)\s*(?:usd|usdc|usdt)\b",
        signal,
        re.IGNORECASE,
    )
    if currency:
        return float(currency.group(1).replace(",", ""))

    return None


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
    """Send a notification message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            print("Telegram notification sent successfully.")
            return True
    except Exception as e:
        print(f"Failed to send Telegram notification: {e}")
        return False


def send_discord_notification(webhook_url: str, message: str) -> bool:
    """Send a notification message via Discord Webhook."""
    payload = {"content": message}
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            print("Discord notification sent successfully.")
            return True
    except Exception as e:
        print(f"Failed to send Discord notification: {e}")
        return False


def create_github_issue(repo_fullname: str, token: str, title: str, body: str) -> bool:
    """Create an issue in the host repository to trigger a native GitHub alert."""
    url = f"https://api.github.com/repos/{repo_fullname}/issues"
    payload = {
        "title": title,
        "body": body,
        "labels": ["bounty-alert"],
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "MyPersonalBountyScout",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            print("GitHub Issue notification created successfully.")
            return True
    except Exception as e:
        print(f"Failed to create GitHub Issue notification: {e}")
        return False


def main() -> None:
    # Load credentials/secrets from environment variables.
    github_token = os.environ.get("GITHUB_TOKEN")
    repo_fullname = os.environ.get("GITHUB_REPOSITORY")

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL")

    seen_urls = load_seen_bounties()
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
        return

    print(f"Discovered {len(new_bounties)} NEW clean paid bounty opportunities!")

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

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
        seen_urls.update(new_bounty_urls)
        if save_seen_bounties(seen_urls):
            print("State saved successfully.")
    else:
        print("No notification was delivered; state not updated so these bounties will be retried.")


if __name__ == "__main__":
    main()
