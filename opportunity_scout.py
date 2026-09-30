import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import scout_bounties as bounty

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
STRATEGIC_GLOBAL_QUERIES = [
    'is:issue is:open no:assignee label:"help wanted" regression sort:updated-desc',
    'is:issue is:open no:assignee label:"help wanted" tests sort:updated-desc',
    'is:issue is:open no:assignee label:"bug" kubernetes sort:updated-desc',
    'is:issue is:open no:assignee label:"bug" networking sort:updated-desc',
]
TARGET_REPO_QUERY_CHUNK = 3
STRATEGIC_VERIFY_LIMIT = 12
REPORT_LIMIT = 8

PAID_DISCOVERY_QUERIES = list(dict.fromkeys(
    bounty.SEARCH_QUERIES + [
        'is:issue is:open "/reward" in:comments sort:updated-desc',
        'is:issue is:open "/bounty" in:comments sort:updated-desc',
        'is:issue is:open "bountyhub.dev" in:title,body,comments sort:updated-desc',
        'is:issue is:open (reward OR compensation OR payout OR "cash prize") "$" in:title,body sort:updated-desc',
    ]
))
EXTENDED_AMOUNT_RE = (
    r"(?:[$€£¥₹R]\s*\d[\d,]*(?:\.\d+)?|"
    r"\d+(?:\.\d+)?\s*(?:usd|usdc|usdt|eur|gbp|cad|aud|nzd|jpy|chf|"
    r"inr|zar|dai|xmr|sol|eth|btc)\b)"
)
PLATFORM_FETCH_LIMIT = 20
ISSUEHUNT_PAGES = 2
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def target_repo_queries():
    queries = []
    for start in range(0, len(TARGET_REPOS), TARGET_REPO_QUERY_CHUNK):
        repos = " ".join(
            f"repo:{repo}"
            for repo in TARGET_REPOS[start:start + TARGET_REPO_QUERY_CHUNK]
        )
        queries.append(f"is:issue is:open no:assignee {repos} sort:updated-desc")
    return queries


def issue_text(item):
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = " ".join(
        str(x.get("name", "")) if isinstance(x, dict) else str(x)
        for x in (item.get("labels") or [])
    ).lower()
    return title, body, labels, f"{title}\n{body}".lower()


def estimate_effort(item):
    title, body, labels, text = issue_text(item)
    if re.search(
        r"\b(?:proposal|epic|roadmap|redesign|rewrite|migration|multi-phase|"
        r"architecture|large refactor|rfc)\b",
        text,
    ):
        return "1d+"
    if re.search(
        r"\b(?:typo|spelling|readme|documentation|docs-only)\b", text
    ) and len(body) < 5000:
        return "<1h"
    if re.search(
        r"\b(?:regression|deterministic|panic|deadlock|race|leak|incorrect|"
        r"failing test|unit test|single|small|narrow|fix)\b",
        f"{title.lower()} {labels} {text[:3000]}",
    ) and int(item.get("comments") or 0) <= 5:
        return "1–3h"
    if len(body) > 12000 or int(item.get("comments") or 0) > 12:
        return "1d+"
    return "3–6h"


def effort_hours(effort):
    return {"<1h": 0.75, "1–3h": 2.0, "3–6h": 4.5, "1d+": 10.0}[effort]


def competition(item):
    comments = int(item.get("comments") or 0)
    if comments == 0:
        return "none"
    if comments <= 3:
        return "low"
    if comments <= 8:
        return "medium"
    return "high"


def payment_confidence(signal):
    if not signal:
        return 0
    if signal.startswith("explicit bounty command"):
        return 100
    if signal.startswith("bounty labels"):
        return 95
    if signal.startswith("named bounty platform"):
        return 90
    return 85


def reward_text(signal):
    if not signal:
        return None
    match = re.search(bounty.AMOUNT_RE, signal, re.IGNORECASE)
    return match.group(0).strip() if match else None


def repo_activity(repo_meta):
    pushed = bounty.parse_github_datetime(repo_meta.get("pushed_at"))
    if not pushed:
        return "unknown"
    days = max(0, (datetime.now(timezone.utc) - pushed).days)
    if days <= 7:
        bucket = "active in last 7d"
    elif days <= 30:
        bucket = "active in last 30d"
    elif days <= 90:
        bucket = "active in last 90d"
    else:
        bucket = f"last push {days}d ago"
    return f"{bucket} ({repo_meta.get('pushed_at')})"


def github_get_optional(url, token):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "OSSOpportunityScout",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def fetch_text(url, timeout=12):
    """Fetch public HTML for official bounty-platform discovery pages."""
    headers = {"User-Agent": "OSSOpportunityScout"}
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"Platform fetch failed for {url}: {exc}")
        return ""


def issue_comments(item, token):
    """Fetch comments for payment verification; basic filters cap threads at 25."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number or not int(item.get("comments") or 0):
        return []
    comments = bounty.github_get(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments?per_page=100",
        token,
    )
    return comments if isinstance(comments, list) else []


def comment_payment_signal(item, token):
    """Recognize confirmed platform comments and trusted bounty commands."""
    comments = issue_comments(item, token)

    # Prefer explicit bot/platform confirmations over the command that triggered them.
    for comment in comments:
        body = str(comment.get("body", ""))
        login = str((comment.get("user") or {}).get("login", "")).lower()
        amount = re.search(EXTENDED_AMOUNT_RE, body, re.IGNORECASE)

        if (
            amount
            and ("algora" in login or "algora.io" in body.lower())
            and re.search(r"\bbounty created\b", body, re.IGNORECASE)
        ):
            return f"confirmed bounty platform comment (Algora): {amount.group(0).strip()}"

        if (
            amount
            and ("opire" in login or "opire.dev" in body.lower())
            and re.search(r"\b(?:reward|bounty)\b", body, re.IGNORECASE)
        ):
            return f"confirmed bounty platform comment (Opire): {amount.group(0).strip()}"

    # Commands alone are accepted only from a repo owner/member/collaborator.
    for comment in comments:
        body = str(comment.get("body", ""))
        association = str(comment.get("author_association", "")).upper()
        if association not in TRUSTED_ASSOCIATIONS:
            continue

        reward = re.search(
            r"/reward\s+(\d[\d,]*(?:\.\d+)?)\b",
            body,
            re.IGNORECASE,
        )
        if reward:
            return f"explicit /reward comment: $\{reward.group(1)}"

        algora = re.search(
            r"/bounty\s+(" + EXTENDED_AMOUNT_RE + r")",
            body,
            re.IGNORECASE,
        )
        if algora:
            return f"explicit /bounty comment: {algora.group(1).strip()}"

    return None


def issue_from_github_url(url, token):
    """Fetch a GitHub source issue from a platform-discovered URL."""
    match = re.match(
        r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)",
        str(url),
    )
    if not match:
        return None
    repo, number = match.group(1), int(match.group(2))
    item = bounty.github_get(
        f"https://api.github.com/repos/{repo}/issues/{number}",
        token,
    )
    return item if isinstance(item, dict) else None


def issuehunt_platform_refs():
    """Read the official IssueHunt funded-issues pages."""
    refs = {}
    for page in range(1, ISSUEHUNT_PAGES + 1):
        url = "https://oss.issuehunt.io/issues"
        if page > 1:
            url += f"?page={page}"
        page_html = fetch_text(url)
        if not page_html:
            continue

        pattern = re.compile(
            r'href=["\'](/r/([^/"\']+)/([^/"\']+)/issues/(\d+))["\']',
            re.IGNORECASE,
        )
        for match in pattern.finditer(page_html):
            owner, repo, number = match.group(2), match.group(3), match.group(4)
            source_url = f"https://github.com/{owner}/{repo}/issues/{number}"
            nearby = page_html[match.end():match.end() + 1200]
            amount = re.search(r"\$\s*\d[\d,]*(?:\.\d+)?", nearby)
            signal = "confirmed bounty platform feed (IssueHunt)"
            if amount:
                signal += f": {amount.group(0).strip()}"
            refs[source_url] = signal
    return refs


def opire_platform_refs():
    """Read visible Opire bounty cards and map them back to GitHub issues."""
    refs = {}
    home = fetch_text("https://app.opire.dev/home")
    if not home:
        return refs

    normalized = home.replace("\\/", "/")
    direct = re.findall(
        r"https://github\.com/[^/\s\"'<>]+/[^/\s\"'<>]+/issues/\d+",
        normalized,
    )
    for source_url in direct[:PLATFORM_FETCH_LIMIT]:
        refs[source_url] = "confirmed bounty platform feed (Opire)"

    detail_paths = list(dict.fromkeys(re.findall(
        r'href=["\'](/issues/[A-Za-z0-9_-]+)["\']',
        normalized,
    )))[:PLATFORM_FETCH_LIMIT]

    for path in detail_paths:
        detail = fetch_text("https://app.opire.dev" + path)
        if not detail:
            continue
        detail = detail.replace("\\/", "/")
        source = re.search(
            r"https://github\.com/[^/\s\"'<>]+/[^/\s\"'<>]+/issues/\d+",
            detail,
        )
        if not source:
            continue
        amount = re.search(
            r"\$\s*\d[\d,]*(?:\.\d+)?\s+bounty\b",
            detail,
            re.IGNORECASE,
        )
        signal = "confirmed bounty platform feed (Opire)"
        if amount:
            signal += f": {amount.group(0).split()[0]}"
        refs[source.group(0)] = signal

    return refs


def platform_paid_refs():
    """Collect official-platform discoveries, deduped by source GitHub issue URL."""
    refs = {}
    for source in (issuehunt_platform_refs(), opire_platform_refs()):
        refs.update(source)
    return refs


def contribution_guide(repo, token):
    for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"):
        data = github_get_optional(
            f"https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path)}",
            token,
        )
        if isinstance(data, dict) and data.get("html_url"):
            return data["html_url"]
    return None


def build_candidate(item, lane, signal, repo_meta, guide):
    repo, number = bounty.issue_repo_and_number(item)
    effort = estimate_effort(item)
    comp = competition(item)
    stars = int(repo_meta.get("stargazers_count") or 0)
    pushed = bounty.parse_github_datetime(repo_meta.get("pushed_at"))
    active_30d = bool(pushed and (datetime.now(timezone.utc) - pushed).days <= 30)
    _, _, labels_text, text = issue_text(item)

    cash = 0
    cash_reasons = []
    amount = bounty.usd_like_amount_from_signal(signal)
    hourly = None
    if lane == "paid":
        confidence = payment_confidence(signal)
        cash += round(confidence * 0.30)
        cash_reasons.append(f"payment confidence {confidence}/100")
        if amount is not None:
            cash += 20 if amount >= 500 else 16 if amount >= 100 else 12 if amount >= 25 else 8 if amount >= 5 else 4
            hourly = amount / effort_hours(effort)
            cash += 25 if hourly >= 100 else 21 if hourly >= 50 else 16 if hourly >= 20 else 10 if hourly >= 10 else 4
            cash_reasons.append(f"~${hourly:.0f}/h expected value")
        else:
            cash_reasons.append("reward not USD-comparable")
        cash += {"none": 15, "low": 11, "medium": 6, "high": 0}[comp]
        if stars >= 1000:
            cash += 7
            cash_reasons.append("established repo")
        elif stars >= 100:
            cash += 4
        if active_30d:
            cash += 3
        cash = max(0, min(100, cash))

    career = 0
    career_reasons = []
    if stars >= 10000:
        career += 20
        career_reasons.append("10k+ star repo")
    elif stars >= 1000:
        career += 17
        career_reasons.append("1k+ star repo")
    elif stars >= 100:
        career += 11
    elif stars >= 10:
        career += 5
    if active_30d:
        career += 10
        career_reasons.append("repo active in last 30d")
    if repo in TARGET_REPOS:
        career += 20
        career_reasons.append("target repo bonus")

    language = str(repo_meta.get("language") or "Unknown")
    lang = language.lower()
    skill = 14 if lang == "go" else 13 if lang in ("typescript", "javascript") else 11 if lang in ("ruby", "php") else 10 if lang == "hcl" else 0
    if skill:
        career_reasons.append(f"{language} codebase")
    infra_terms = (
        "kubernetes", "aws", "network", "dns", "proxy", "routing",
        "observability", "prometheus", "otel", "distributed", "controller",
        "terraform", "gitops", "backend", "api", "concurrency",
    )
    if any(term in text for term in infra_terms):
        skill += 6
        career_reasons.append("target infrastructure/domain fit")
    career += min(20, skill)

    depth_terms = (
        "race", "deadlock", "concurrency", "network", "protocol", "controller",
        "distributed", "storage", "performance", "memory", "leak", "api",
    )
    depth = sum(term in text for term in depth_terms)
    career += 15 if depth >= 3 else 9 if depth >= 1 else 5 if re.search(r"\b(?:test|regression|bug|fix)\b", text) else 0
    if depth:
        career_reasons.append("meaningful technical depth")

    merge = 0
    if re.search(r"\b(?:test|tests|regression)\b", text):
        merge += 5
        career_reasons.append("tests/regression signal")
    if "help wanted" in labels_text or "good first issue" in labels_text:
        merge += 4
        career_reasons.append("maintainer contributor signal")
    if guide:
        merge += 3
        career_reasons.append("contribution guide found")
    if effort in ("<1h", "1–3h"):
        merge += 8
        career_reasons.append("bounded implementation scope")
    elif effort == "3–6h":
        merge += 4
    career += min(20, merge)
    career -= {"none": 0, "low": 2, "medium": 7, "high": 15}[comp]
    if effort == "1d+":
        career -= 8
        career_reasons.append("large-scope penalty")
    career = max(0, min(100, career))

    priority = career if lane == "strategic" else min(
        100, max(cash, career) + (5 if cash >= 70 and career >= 70 else 0)
    )
    labels = [
        str(x.get("name", "")) if isinstance(x, dict) else str(x)
        for x in (item.get("labels") or [])
    ]
    return {
        "repo": repo,
        "issue_number": number,
        "title": item.get("title"),
        "url": item.get("html_url"),
        "paid": lane == "paid",
        "reward": reward_text(signal),
        "payment_confidence": payment_confidence(signal),
        "cash_score": cash,
        "career_score": career,
        "priority_score": priority,
        "effort": effort,
        "expected_hourly": hourly,
        "competition": comp,
        "stars": stars,
        "recent_activity": repo_activity(repo_meta),
        "language": language,
        "labels": labels,
        "cash_reasons": cash_reasons[:6],
        "career_reasons": career_reasons[:8],
        "contribution_guide": guide,
        "comments": int(item.get("comments") or 0),
        "updated_at": item.get("updated_at"),
        "rejection_reason": None,
    }


def refresh_issue(item, token):
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number:
        return None, "could not identify repository/issue number"
    fresh = bounty.github_get(f"https://api.github.com/repos/{repo}/issues/{number}", token)
    if not isinstance(fresh, dict):
        return None, "could not refresh source issue"
    if fresh.get("state") != "open":
        return None, "issue is no longer open"
    if "pull_request" in fresh:
        return None, "source is a pull request, not an issue"
    return fresh, None


def strategic_rejection(item, token):
    if not bounty.is_clean_candidate(item):
        return "failed basic eligibility filter"
    _, _, labels, text = issue_text(item)
    if "oss opportunity queue" in text:
        return "generated opportunity-scout report"
    if any(x in labels for x in (
        "question", "support", "needs info", "needs-info", "needs-information",
        "waiting for info", "waiting-for-info", "invalid",
    )):
        return "support/triage issue rather than a contributor task"
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number:
        return "could not identify repository/issue number"
    reason = bounty.has_existing_implementation_pr(repo, number, token)
    if reason:
        return reason
    return bounty.active_claim_reason(repo, number, int(item.get("comments") or 0), token)


def verify(item, token, repo_cache, guide_cache, require_paid=False):
    fresh, reason = refresh_issue(item, token)
    if reason:
        return None, reason
    if not bounty.is_clean_candidate(fresh):
        return None, "failed basic eligibility filter after source refresh"

    signal = bounty.payment_signal(fresh)
    if require_paid or signal:
        reason, signal = bounty.candidate_rejection_reason(fresh, token)
        if reason:
            return None, reason
        lane = "paid"
    else:
        reason = strategic_rejection(fresh, token)
        if reason:
            return None, reason
        lane = "strategic"

    repo, _ = bounty.issue_repo_and_number(fresh)
    if repo not in repo_cache:
        repo_cache[repo] = bounty.fetch_repo_metadata(repo, token)
    repo_meta = repo_cache[repo]
    if not repo_meta:
        return None, "repository metadata unavailable"
    if repo_meta.get("archived"):
        return None, "repository is archived"
    if repo not in guide_cache:
        guide_cache[repo] = contribution_guide(repo, token)
    return build_candidate(fresh, lane, signal, repo_meta, guide_cache[repo]), None


def add_reject(counts, examples, item, reason):
    counts[reason] = counts.get(reason, 0) + 1
    if len(examples) < 12:
        examples.append({"url": item.get("html_url"), "title": item.get("title"), "reason": reason})


def discover_paid(token, seen, repo_cache, guide_cache):
    found, touched, rejected, examples = [], set(), {}, []
    for query in bounty.SEARCH_QUERIES:
        for item in bounty.search_github(query, token).get("items", []):
            url = item.get("html_url")
            if not url or url in seen or url in touched:
                continue
            touched.add(url)
            if not bounty.is_clean_candidate(item):
                continue
            candidate, reason = verify(item, token, repo_cache, guide_cache, True)
            if reason:
                add_reject(rejected, examples, item, reason)
                print(f"Skipping paid candidate {url}: {reason}")
            else:
                found.append(candidate)
    return found, rejected, examples


def discover_strategic(token, seen, paid_urls, repo_cache, guide_cache):
    provisional, touched, rejected, examples = [], set(), {}, []
    for query in target_repo_queries() + STRATEGIC_GLOBAL_QUERIES:
        for item in bounty.search_github(query, token, per_page=12).get("items", []):
            url = item.get("html_url")
            if not url or url in seen or url in paid_urls or url in touched:
                continue
            touched.add(url)
            if not bounty.is_clean_candidate(item):
                continue
            repo, _ = bounty.issue_repo_and_number(item)
            if not repo:
                continue
            if repo not in repo_cache:
                repo_cache[repo] = bounty.fetch_repo_metadata(repo, token)
            meta = repo_cache[repo]
            if not meta or meta.get("archived"):
                continue
            signal = bounty.payment_signal(item)
            lane = "paid" if signal else "strategic"
            preview = build_candidate(item, lane, signal, meta, None)
            provisional.append((preview["priority_score"], preview["career_score"], preview["cash_score"], item))

    provisional.sort(key=lambda row: row[:3], reverse=True)
    found = []
    for _, _, _, item in provisional[:STRATEGIC_VERIFY_LIMIT]:
        candidate, reason = verify(item, token, repo_cache, guide_cache)
        if reason:
            add_reject(rejected, examples, item, reason)
            print(f"Skipping strategic candidate {item.get('html_url')}: {reason}")
        else:
            found.append(candidate)
    return found, rejected, examples


def markdown_candidate(candidate, idx):
    lane = "Paid" if candidate["paid"] else "Strategic OSS"
    hourly = f"~${candidate['expected_hourly']:.0f}/h" if candidate["expected_hourly"] is not None else "unknown / not USD-comparable"
    guide = f"[contribution guide]({candidate['contribution_guide']})" if candidate["contribution_guide"] else "not found at common paths"
    return (
        f"#### {idx}. [{candidate['repo']} #{candidate['issue_number']}: {candidate['title']}]({candidate['url']})\n"
        f"- **Paid / unpaid:** {lane}\n"
        f"- **Reward:** {candidate['reward'] or 'none'}\n"
        f"- **Payment confidence:** {candidate['payment_confidence']}/100\n"
        f"- **Cash score:** {candidate['cash_score']}/100\n"
        f"- **Career score:** {candidate['career_score']}/100\n"
        f"- **Effort:** {candidate['effort']}\n"
        f"- **Expected hourly value:** {hourly}\n"
        f"- **Competition:** {candidate['competition']}\n"
        f"- **Repo stars:** {candidate['stars']}\n"
        f"- **Repo recent activity:** {candidate['recent_activity']}\n"
        f"- **Language:** {candidate['language']}\n"
        f"- **Labels:** {', '.join(candidate['labels']) or 'none'}\n"
        f"- **Contribution process:** {guide}\n"
        f"- **Cash reasons:** {', '.join(candidate['cash_reasons']) or 'unpaid lane'}\n"
        f"- **Career reasons:** {', '.join(candidate['career_reasons'])}\n"
        f"- **Rejection reason:** none\n\n"
    )


def notification_candidate(candidate, idx):
    title = str(candidate["title"] or "")
    if len(title) > 100:
        title = title[:97] + "..."
    return [
        f"{idx}. *{candidate['repo']} #{candidate['issue_number']}* — {title}",
        f"   • {'paid' if candidate['paid'] else 'strategic OSS'} | reward: {candidate['reward'] or 'none'}",
        f"   • Cash {candidate['cash_score']}/100 | Career {candidate['career_score']}/100",
        f"   • {candidate['effort']} | competition: {candidate['competition']}",
        f"   • {candidate['url']}",
    ]


def main():
    token = os.environ.get("GITHUB_TOKEN")
    repo_fullname = os.environ.get("GITHUB_REPOSITORY")
    seen = bounty.load_seen_bounties()
    repo_cache, guide_cache = {}, {}

    paid, paid_rejects, paid_examples = discover_paid(token, seen, repo_cache, guide_cache)
    strategic, strategic_rejects, strategic_examples = discover_strategic(
        token, seen, {x["url"] for x in paid}, repo_cache, guide_cache
    )

    by_url = {}
    for candidate in paid + strategic:
        old = by_url.get(candidate["url"])
        if not old or candidate["priority_score"] > old["priority_score"]:
            by_url[candidate["url"]] = candidate
    queue = sorted(
        by_url.values(),
        key=lambda x: (x["priority_score"], x["career_score"], x["cash_score"], -x["comments"]),
        reverse=True,
    )[:REPORT_LIMIT]
    if not queue:
        print("No new verified OSS opportunities found.")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"🎯 *OSS Opportunity Queue* ({now})", ""]
    for idx, candidate in enumerate(queue, 1):
        lines.extend(notification_candidate(candidate, idx))
        lines.append("")
    message = "\n".join(lines)

    attempted = False
    delivered = False
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if telegram_token and telegram_chat_id:
        attempted = True
        delivered = bounty.send_telegram_notification(telegram_token, telegram_chat_id, message) or delivered
    if discord_webhook:
        attempted = True
        delivered = bounty.send_discord_notification(discord_webhook, message.replace("•", "-")) or delivered

    if token and repo_fullname:
        attempted = True
        body = (
            f"### Ranked OSS Opportunity Queue\n\n**Scan Time:** {now}\n\n"
            "Paid candidates reuse BountyScout's existing payment/competition filters unchanged. "
            "Strategic candidates are pre-ranked, then source-refreshed and checked for assignees, "
            "claim comments, open implementation PRs, repository legitimacy, and contribution guidance.\n\n"
        )
        for idx, candidate in enumerate(queue, 1):
            body += markdown_candidate(candidate, idx)
        examples = (paid_examples + strategic_examples)[:12]
        if examples:
            body += "### Verification rejects\n\n"
            for item in examples:
                body += f"- [{item['title'] or item['url']}]({item['url']}): {item['reason']}\n"
        delivered = bounty.create_github_issue(
            repo_fullname,
            token,
            f"🎯 OSS Opportunity Queue: {len(queue)} new verified candidate{'s' if len(queue) != 1 else ''}",
            body,
        ) or delivered

    rejects = dict(paid_rejects)
    for reason, count in strategic_rejects.items():
        rejects[reason] = rejects.get(reason, 0) + count
    if rejects:
        print("Filtered verified candidates: " + ", ".join(f"{k}={v}" for k, v in sorted(rejects.items())))

    if attempted and delivered:
        seen.update(x["url"] for x in queue)
        bounty.save_seen_bounties(seen)
    else:
        print("No notification was delivered; state was not updated.")


if __name__ == "__main__":
    main()
