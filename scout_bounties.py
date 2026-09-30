import json
import os
import urllib.request
import urllib.parse
import re
from datetime import datetime, timezone

# Configuration
STATE_FILE = "seen_bounties.json"
MAX_COMMENTS = 25  # Filter out overcrowded threads

# Lane 1: explicit paid bounty discovery.
PAID_SEARCH_QUERIES = [
    'is:issue is:open bounty in:title,body sort:updated-desc',
    'is:issue is:open reward bounty sort:updated-desc',
    'is:issue is:open "paid" "PR" "bounty" sort:updated-desc',
    'is:issue is:open "Opire" bounty sort:updated-desc',
]

# Lane 2: respected infrastructure/backend repositories. Repo qualifiers are
# grouped into small chunks so we do not spend one Search API request per repo.
TARGET_REPOS = [
    "aws/amazon-vpc-cni-k8s",
    "kubernetes/kubernetes",
    "kubernetes-sigs/controller-runtime",
    "kubernetes-sigs/external-dns",
    "kubernetes-sigs/aws-load-balancer-controller",
    "tailscale/tailscale",
    "cilium/cilium",
    "prometheus/prometheus",
    "prometheus/client_golang",
    "grafana/loki",
    "open-telemetry/opentelemetry-go",
    "hashicorp/terraform",
    "fluxcd/flux2",
    "argoproj/argo-cd",
    "golangci/golangci-lint",
]
TARGET_REPO_QUERY_CHUNK = 3
STRATEGIC_VERIFY_LIMIT = 12
REPORT_LIMIT = 8

# Global discovery stays intentionally narrow. These are supplements to the
# target-repo lane, not a generic "good first issue" scraper.
STRATEGIC_GLOBAL_QUERIES = [
    'is:issue is:open no:assignee label:"help wanted" regression sort:updated-desc',
    'is:issue is:open no:assignee label:"help wanted" tests sort:updated-desc',
    'is:issue is:open no:assignee label:"bug" kubernetes sort:updated-desc',
    'is:issue is:open no:assignee label:"bug" networking sort:updated-desc',
]

# Strong payment signals. A plain mention of "bounty", "paid", or "reward" is
# deliberately not enough because those words appear in unrelated issues.
PAYMENT_TERM_RE = r"(?:bounty|reward|payout|compensation|pay(?:ment|s|ing|s)?|paid)"
AMOUNT_RE = r"(?:[$€£]\s*\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:usd|usdc|usdt|eur|gbp|xmr|sol|eth|btc)\b)"

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


def load_seen_bounties():
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


def save_seen_bounties(seen_urls):
    """Save the updated list of seen bounty URLs."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen_urls), f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving state file: {e}")
        return False


def github_get(url, token=None, timeout=20):
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


def search_github(query, token=None, per_page=15):
    """Fetch search results from GitHub Issues API."""
    url = f"https://api.github.com/search/issues?{urllib.parse.urlencode({'q': query, 'per_page': per_page})}"
    data = github_get(url, token)
    return data if isinstance(data, dict) else {}


def payment_signal(item):
    """Return a strong payment signal, or None when payment is not explicit."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = item.get("labels") or []
    label_names = [
        str(label.get("name", "")) if isinstance(label, dict) else str(label)
        for label in labels
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
    if re.search(AMOUNT_RE, labels_text, re.IGNORECASE) and re.search(
        r"\b(?:bounty|reward|payout)\b", labels_text, re.IGNORECASE
    ):
        amount = re.search(AMOUNT_RE, labels_text, re.IGNORECASE)
        return f"bounty labels: {amount.group(0).strip()}"

    # Platform-backed language can be explicit even when the exact amount is
    # stored outside the issue body.
    if re.search(r"\b(?:algora|opire)\b", lower) and re.search(
        r"\b(?:funded|bounty|reward|payout)\b", lower
    ):
        return "named bounty platform + funding language"

    return None


def issue_repo_and_number(item):
    """Extract owner/repo and issue number from a GitHub issue search item."""
    url = str(item.get("html_url", ""))
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)", url)
    if not match:
        return None, None
    return match.group(1), int(match.group(2))


def has_existing_implementation_pr(repo, issue_number, token):
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


def active_claim_reason(repo, issue_number, comments_count, token):
    """Return a reason when recent comments clearly claim or implement the task."""
    if not comments_count:
        return None

    params = urllib.parse.urlencode({
        "per_page": min(int(comments_count), 30),
        "sort": "created",
        "direction": "desc",
    })
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


def is_clean_candidate(item):
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
        "oss opportunity queue",
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
        "airdrop", "referral", "casino", "gambling", "trading bot",
        "blog post", "article writing", "tutorial proposal", "content creator",
    ]
    if any(term in title or term in body for term in blocklist):
        return False

    return True


def candidate_rejection_reason(item, token):
    """Apply strict money + competition checks and return a rejection reason."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = item.get("labels") or []
    label_names = [
        str(label.get("name", "")) if isinstance(label, dict) else str(label)
        for label in labels
    ]
    combined = f"{title}\n{body}"
    lower_combined = combined.lower()
    lower_labels = " ".join(label_names).lower()

    if any(
        re.search(pattern, combined, re.IGNORECASE)
        for pattern in UNFUNDED_PROPOSAL_PATTERNS
    ):
        return "unfunded bounty proposal, not an existing award", None

    if any(
        marker in lower_combined or marker in lower_labels
        for marker in META_ALERT_MARKERS
    ):
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


def parse_github_datetime(value):
    """Parse a GitHub ISO timestamp, returning None when unavailable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def usd_like_amount_from_signal(signal):
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


def fetch_repo_metadata(repo, token):
    """Fetch lightweight repository metadata used only for ranking."""
    data = github_get(f"https://api.github.com/repos/{repo}", token)
    return data if isinstance(data, dict) else {}


def target_repo_search_queries():
    """Build grouped target-repository searches to conserve Search API calls."""
    queries = []
    for start in range(0, len(TARGET_REPOS), TARGET_REPO_QUERY_CHUNK):
        chunk = TARGET_REPOS[start:start + TARGET_REPO_QUERY_CHUNK]
        repos = " ".join(f"repo:{repo}" for repo in chunk)
        queries.append(
            f"is:issue is:open no:assignee {repos} sort:updated-desc"
        )
    return queries


def issue_text_and_labels(item):
    """Return normalized issue text and labels for scoring heuristics."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = item.get("labels") or []
    label_names = [
        str(label.get("name", "")) if isinstance(label, dict) else str(label)
        for label in labels
    ]
    return title, body, " ".join(label_names).lower(), f"{title}\n{body}".lower()


def payment_confidence(signal):
    """Return a transparent 0-100 payment-confidence score."""
    if not signal:
        return 0
    if signal.startswith("explicit bounty command"):
        return 100
    if signal.startswith("bounty labels"):
        return 95
    if signal.startswith("named bounty platform"):
        return 90
    return 85


def reward_amount_text(signal):
    """Return the stated reward token, preserving currency when available."""
    if not signal:
        return None
    match = re.search(AMOUNT_RE, signal, re.IGNORECASE)
    return match.group(0).strip() if match else None


def repo_activity(repo_meta):
    """Summarize recent repository activity from pushed_at."""
    pushed_at = parse_github_datetime(repo_meta.get("pushed_at"))
    if not pushed_at:
        return "unknown"
    days = max(0, (datetime.now(timezone.utc) - pushed_at).days)
    if days <= 7:
        return f"active in last 7d ({repo_meta.get('pushed_at')})"
    if days <= 30:
        return f"active in last 30d ({repo_meta.get('pushed_at')})"
    if days <= 90:
        return f"active in last 90d ({repo_meta.get('pushed_at')})"
    return f"last push {days}d ago ({repo_meta.get('pushed_at')})"


def estimate_effort(item):
    """Estimate implementation effort using deliberately coarse buckets."""
    title, body, labels_text, text = issue_text_and_labels(item)
    comments = int(item.get("comments") or 0)

    broad = re.search(
        r"\b(?:proposal|epic|roadmap|redesign|rewrite|migration|multi-phase|"
        r"architecture|new subsystem|large refactor|rfc)\b",
        text,
    )
    tiny = re.search(
        r"\b(?:typo|spelling|readme|documentation|docs-only|comment-only)\b",
        text,
    )
    bounded = re.search(
        r"\b(?:regression|deterministic|panic|deadlock|race|leak|incorrect|"
        r"failing test|unit test|single|small|narrow|fix)\b",
        f"{title.lower()} {labels_text} {text[:3000]}",
    )

    if broad:
        return "1d+"
    if tiny and len(body) < 5000:
        return "<1h"
    if bounded and comments <= 5:
        return "1–3h"
    if len(body) > 12000 or comments > 12:
        return "1d+"
    return "3–6h"


def effort_hours(effort):
    """Return a midpoint used only for rough expected-value scoring."""
    return {
        "<1h": 0.75,
        "1–3h": 2.0,
        "3–6h": 4.5,
        "1d+": 10.0,
    }.get(effort, 4.5)


def competition_level(item):
    """Estimate residual competition after hard claim/PR checks pass."""
    comments = int(item.get("comments") or 0)
    if comments == 0:
        return "none"
    if comments <= 3:
        return "low"
    if comments <= 8:
        return "medium"
    return "high"


def github_get_optional(url, token=None, timeout=10):
    """Fetch optional GitHub JSON without logging expected 404s."""
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
    except Exception:
        return None


def find_contribution_guide(repo, token):
    """Find a contribution guide at common GitHub locations."""
    for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"):
        encoded = urllib.parse.quote(path)
        data = github_get_optional(
            f"https://api.github.com/repos/{repo}/contents/{encoded}",
            token,
        )
        if isinstance(data, dict) and data.get("html_url"):
            return data["html_url"]
    return None


def score_cash(item, signal, repo_meta, effort, competition):
    """Score paid work by payment confidence, reward, time, and competition."""
    score = 0
    reasons = []

    confidence = payment_confidence(signal)
    score += round(confidence * 0.30)
    reasons.append(f"payment confidence {confidence}/100")

    amount = usd_like_amount_from_signal(signal)
    if amount is not None:
        if amount >= 500:
            score += 20
            reasons.append("$500+ stated reward")
        elif amount >= 100:
            score += 16
            reasons.append("$100+ stated reward")
        elif amount >= 25:
            score += 12
            reasons.append("$25+ stated reward")
        elif amount >= 5:
            score += 8
            reasons.append("$5+ stated reward")
        elif amount > 0:
            score += 4
            reasons.append("micro-bounty")

        hourly = amount / effort_hours(effort)
        if hourly >= 100:
            score += 25
            reasons.append("very high expected hourly value")
        elif hourly >= 50:
            score += 21
            reasons.append("high expected hourly value")
        elif hourly >= 20:
            score += 16
            reasons.append("solid expected hourly value")
        elif hourly >= 10:
            score += 10
            reasons.append("reasonable expected hourly value")
        elif hourly > 0:
            score += 4
            reasons.append("low expected hourly value")
    else:
        reasons.append("reward amount not USD-comparable")

    competition_points = {
        "none": 15,
        "low": 11,
        "medium": 6,
        "high": 0,
    }.get(competition, 0)
    score += competition_points
    reasons.append(f"{competition} residual competition")

    stars = int(repo_meta.get("stargazers_count") or 0)
    pushed_at = parse_github_datetime(repo_meta.get("pushed_at"))
    active_30d = bool(
        pushed_at and (datetime.now(timezone.utc) - pushed_at).days <= 30
    )
    if stars >= 1000:
        score += 7
        reasons.append("established repo")
    elif stars >= 100:
        score += 4
    if active_30d:
        score += 3
        reasons.append("repo active in last 30d")

    return max(0, min(100, score)), reasons[:7]


def score_career(item, repo, repo_meta, effort, competition, contribution_url):
    """Score public OSS value, skill fit, depth, and likely mergeability."""
    score = 0
    reasons = []
    title, _, labels_text, text = issue_text_and_labels(item)

    stars = int(repo_meta.get("stargazers_count") or 0)
    if stars >= 10000:
        score += 20
        reasons.append("10k+ star repo")
    elif stars >= 1000:
        score += 17
        reasons.append("1k+ star repo")
    elif stars >= 100:
        score += 11
        reasons.append("100+ star repo")
    elif stars >= 10:
        score += 5

    pushed_at = parse_github_datetime(repo_meta.get("pushed_at"))
    if pushed_at:
        age_days = (datetime.now(timezone.utc) - pushed_at).days
        if age_days <= 30:
            score += 10
            reasons.append("repo active in last 30d")
        elif age_days <= 90:
            score += 5

    if repo in TARGET_REPOS:
        score += 20
        reasons.append("target repo bonus")

    language = str(repo_meta.get("language") or "Unknown")
    language_lower = language.lower()
    skill_points = 0
    if language_lower == "go":
        skill_points += 14
        reasons.append("Go codebase")
    elif language_lower in ("typescript", "javascript"):
        skill_points += 13
        reasons.append(f"{language} codebase")
    elif language_lower in ("ruby", "php"):
        skill_points += 11
        reasons.append(f"{language} codebase")
    elif language_lower == "hcl":
        skill_points += 10
        reasons.append("Terraform/HCL codebase")

    infra_terms = (
        "kubernetes", "aws", "network", "dns", "proxy", "routing",
        "observability", "prometheus", "otel", "distributed", "controller",
        "terraform", "gitops", "backend", "api", "concurrency",
    )
    if any(term in text for term in infra_terms):
        skill_points += 6
        reasons.append("target infrastructure/domain fit")
    score += min(20, skill_points)

    depth_terms = (
        "race", "deadlock", "concurrency", "network", "protocol", "scheduler",
        "controller", "reconciliation", "distributed", "storage", "cache",
        "performance", "memory", "leak", "api", "authentication",
    )
    depth_hits = sum(1 for term in depth_terms if term in text)
    if depth_hits >= 3:
        score += 15
        reasons.append("strong technical depth")
    elif depth_hits >= 1:
        score += 9
        reasons.append("meaningful technical depth")
    elif re.search(r"\b(?:test|regression|bug|fix)\b", text):
        score += 5

    merge_points = 0
    if re.search(r"\b(?:test|tests|regression)\b", text):
        merge_points += 5
        reasons.append("tests/regression signal")
    if any(term in labels_text for term in ("help wanted", "good first issue")):
        merge_points += 4
        reasons.append("maintainer contributor signal")
    if contribution_url:
        merge_points += 3
        reasons.append("contribution guide found")
    if effort in ("<1h", "1–3h"):
        merge_points += 8
        reasons.append("bounded implementation scope")
    elif effort == "3–6h":
        merge_points += 4
    score += min(20, merge_points)

    competition_penalty = {
        "none": 0,
        "low": 2,
        "medium": 7,
        "high": 15,
    }.get(competition, 0)
    score -= competition_penalty
    if competition_penalty:
        reasons.append(f"{competition} competition penalty")

    if re.search(
        r"\b(?:proposal|epic|roadmap|redesign|rewrite|rfc)\b",
        f"{title.lower()} {text[:4000]}",
    ):
        score -= 10
        reasons.append("broad/design-heavy scope")

    return max(0, min(100, score)), reasons[:8]


def opportunity_priority(lane, cash_score, career_score):
    """Rank candidates without collapsing the two user-facing scores."""
    if lane == "paid":
        bonus = 5 if cash_score >= 70 and career_score >= 70 else 0
        return min(100, max(cash_score, career_score) + bonus)
    return career_score


def build_candidate(item, lane, signal, repo_meta, contribution_url):
    """Build the normalized candidate record used by every output channel."""
    repo, issue_number = issue_repo_and_number(item)
    effort = estimate_effort(item)
    competition = competition_level(item)
    cash_score, cash_reasons = (0, [])
    if lane == "paid":
        cash_score, cash_reasons = score_cash(
            item,
            signal,
            repo_meta,
            effort,
            competition,
        )
    career_score, career_reasons = score_career(
        item,
        repo,
        repo_meta,
        effort,
        competition,
        contribution_url,
    )

    return {
        "repo": repo,
        "issue_number": issue_number,
        "title": item.get("title"),
        "url": item.get("html_url"),
        "paid": lane == "paid",
        "lane": lane,
        "reward": reward_amount_text(signal),
        "payment_signal": signal,
        "payment_confidence": payment_confidence(signal),
        "cash_score": cash_score,
        "career_score": career_score,
        "priority_score": opportunity_priority(lane, cash_score, career_score),
        "effort": effort,
        "competition": competition,
        "stars": int(repo_meta.get("stargazers_count") or 0),
        "recent_activity": repo_activity(repo_meta),
        "language": str(repo_meta.get("language") or "Unknown"),
        "labels": [
            str(label.get("name", "")) if isinstance(label, dict) else str(label)
            for label in (item.get("labels") or [])
        ],
        "cash_reasons": cash_reasons,
        "career_reasons": career_reasons,
        "contribution_guide": contribution_url,
        "comments": int(item.get("comments") or 0),
        "updated_at": item.get("updated_at"),
        "rejection_reason": None,
    }


def send_telegram_notification(token, chat_id, message):
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


def send_discord_notification(webhook_url, message):
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


def create_github_issue(repo_fullname, token, title, body):
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


def main():
    # Load credentials/secrets from environment variables.
    github_token = os.environ.get("GITHUB_TOKEN")
    repo_fullname = os.environ.get("GITHUB_REPOSITORY")

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL")

    seen_urls = load_seen_bounties()
    new_bounties = []
    new_bounty_urls = set()
    rejected = {}
    repo_metadata_cache = {}

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
            if repo not in repo_metadata_cache:
                repo_metadata_cache[repo] = fetch_repo_metadata(repo, github_token)
            repo_meta = repo_metadata_cache[repo]

            score, tier, score_reasons = score_candidate(item, signal, repo_meta)
            new_bounties.append({
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
            })
            new_bounty_urls.add(url)

    if rejected:
        summary = ", ".join(
            f"{reason}={count}" for reason, count in sorted(rejected.items())
        )
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
        notif_lines.append(
            f"   • Scout score: {bounty['score']}/100 ({bounty['tier']})"
        )
        notif_lines.append(f"   • Payment signal: {bounty['payment_signal']}")
        notif_lines.append(f"   • Repo stars: {bounty['stars']}")
        notif_lines.append(
            f"   • Why: {', '.join(bounty['score_reasons'])}"
        )
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
            send_discord_notification(discord_webhook, discord_msg)
            or notification_succeeded
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
        print(
            "No notification was delivered; state not updated so these "
            "bounties will be retried."
        )


if __name__ == "__main__":
    main()
