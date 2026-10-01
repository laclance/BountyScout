from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping, cast

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
    "containerd/containerd",
    "moby/moby",
    "grpc/grpc-go",
    "etcd-io/etcd",
    "cloudflare/cloudflared",
    "traefik/traefik",
    "open-telemetry/opentelemetry-js",
    "nodejs/undici",
]
STRATEGIC_GLOBAL_QUERIES = [
    'is:issue is:open no:assignee label:"help wanted" regression sort:updated-desc',
    'is:issue is:open no:assignee label:"help wanted" tests sort:updated-desc',
    'is:issue is:open no:assignee label:"help wanted" panic sort:updated-desc',
    'is:issue is:open no:assignee label:"help wanted" deadlock sort:updated-desc',
    'is:issue is:open no:assignee label:"help wanted" bug sort:updated-desc',
    'is:issue is:open no:assignee label:"Status: Help Wanted" bug sort:updated-desc',
    'is:issue is:open no:assignee label:"Help-Wanted" bug sort:updated-desc',
    'is:issue is:open no:assignee label:"contributor/help-wanted" bug sort:updated-desc',
    'is:issue is:open no:assignee label:"good first issue" bug sort:updated-desc',
    'is:issue is:open no:assignee label:"bug" kubernetes sort:updated-desc',
    'is:issue is:open no:assignee label:"bug" networking sort:updated-desc',
]
TARGET_REPO_QUERY_CHUNK = 3
STRATEGIC_SEARCH_PER_PAGE = 20
STRATEGIC_VERIFY_LIMIT = 20
STRATEGIC_VERIFY_PER_REPO = 3
STRATEGIC_MIN_CAREER_SCORE = 55
REPORT_LIMIT = 8

PAID_DISCOVERY_QUERIES = list(
    dict.fromkeys(
        bounty.SEARCH_QUERIES
        + [
            'is:issue is:open "/reward" in:comments sort:updated-desc',
            'is:issue is:open "/bounty" in:comments sort:updated-desc',
            "is:issue is:open (opire.dev OR bountyhub.dev OR algora.io) in:comments sort:updated-desc",
            'is:issue is:open (reward OR compensation OR payout OR "cash prize") "$" in:title,body sort:updated-desc',
        ]
    )
)
EXTENDED_AMOUNT_RE = (
    r"(?:[$€£¥₹]\s*\d[\d,]*(?:\.\d+)?|"
    r"(?<![A-Za-z])R\s*\d[\d,]*(?:\.\d+)?|"
    r"\d+(?:\.\d+)?\s*(?:usd|usdc|usdt|eur|gbp|cad|aud|nzd|jpy|chf|"
    r"inr|zar|dai|xmr|sol|eth|btc)\b)"
)
PLATFORM_FETCH_LIMIT = 20
ISSUEHUNT_PAGES = 2
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def target_repo_queries() -> list[str]:
    queries = []
    for start in range(0, len(TARGET_REPOS), TARGET_REPO_QUERY_CHUNK):
        repos = " ".join(
            f"repo:{repo}" for repo in TARGET_REPOS[start : start + TARGET_REPO_QUERY_CHUNK]
        )
        queries.append(f"is:issue is:open no:assignee {repos} sort:updated-desc")
    return queries


def issue_text(item: Mapping[str, Any]) -> tuple[str, str, str, str]:
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = " ".join(
        str(x.get("name", "")) if isinstance(x, dict) else str(x)
        for x in (item.get("labels") or [])
    ).lower()
    return title, body, labels, f"{title}\n{body}".lower()


CODE_FILE_RE = re.compile(
    r"(?<![\w.-])(?:[\w.-]+/)*[\w.-]+\.(?:go|sh|py|yaml|yml)\b",
    re.IGNORECASE,
)


def code_reference_count(text: str) -> int:
    return len({match.group(0).lower() for match in CODE_FILE_RE.finditer(text)})


def estimate_effort(item: Mapping[str, Any]) -> str:
    title, body, labels, text = issue_text(item)
    comments = int(item.get("comments") or 0)
    file_refs = code_reference_count(body)

    if (
        "kind/feature" in labels
        or title.lower().startswith(("fr:", "feature request:"))
        or re.search(
            r"\b(?:propos(?:e|ed|ing|al)|epic|roadmap|redesign|rewrite|"
            r"migration|multi-phase|architecture|large refactor|rfc|"
            r"connection pool|add support|explore publishing)\b",
            text,
        )
        or re.search(
            r"\b(?:dual[ -]?sim|physical device|device-specific|hardware-dependent)\b",
            text,
        )
        or len(body) > 12000
        or comments > 12
    ):
        return "1d+"

    if (
        re.search(r"\b(?:typo|spelling|readme|documentation|docs-only)\b", text)
        and len(body) < 5000
    ):
        return "<1h"

    mobile_or_desktop = any(
        marker in labels for marker in ("os-android", "os-ios", "os-macos", "os-windows")
    )
    missing_reproduction = "_no response_" in text or "no response" in text
    suggested_fix_bullets = len(re.findall(r"(?m)^\s*-\s+", body))
    if (
        file_refs >= 4
        or len(body) > 8500
        or (mobile_or_desktop and missing_reproduction)
        or (re.search(r"\bsuggested fix(?:es)?\b", text) and suggested_fix_bullets >= 3)
    ):
        return "6–12h"

    bounded = re.search(
        r"\b(?:regression|deterministic|panics?|deadlocks?|races?|"
        r"leaks?|incorrect|failing tests?|unit tests?|single|small|narrow|"
        r"no-op|stale|fix(?:es|ed|ing)?)\b|"
        r"\bnever closes\b|\bevery sync\b",
        f"{title.lower()} {labels} {text[:4500]}",
    )
    if bounded and comments <= 3 and len(body) < 4500 and file_refs <= 2:
        return "1–3h"

    return "3–6h"


def effort_hours(effort: str) -> float:
    return {
        "<1h": 0.75,
        "1–3h": 2.0,
        "3–6h": 4.5,
        "6–12h": 9.0,
        "1d+": 16.0,
    }[effort]


def competition(item: Mapping[str, Any]) -> str:
    comments = int(item.get("comments") or 0)
    if comments == 0:
        return "none"
    if comments <= 3:
        return "low"
    if comments <= 8:
        return "medium"
    return "high"


def payment_confidence(signal: str | None) -> int:
    if not signal:
        return 0
    if signal.startswith("confirmed bounty platform"):
        return 100
    if signal.startswith("explicit bounty command"):
        return 100
    if signal.startswith("explicit /reward comment"):
        return 98
    if signal.startswith("explicit /bounty comment"):
        return 98
    if signal.startswith("bounty labels"):
        return 95
    if signal.startswith("named bounty platform"):
        return 90
    return 85


def reward_text(signal: str | None) -> str | None:
    if not signal:
        return None
    match = re.search(EXTENDED_AMOUNT_RE, signal, re.IGNORECASE)
    return match.group(0).strip() if match else None


def repo_activity(repo_meta: Mapping[str, Any]) -> str:
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


def github_get_optional(url: str, token: str | None) -> Any:
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


def fetch_text(url: str, timeout: int = 12) -> str:
    """Fetch public HTML for official bounty-platform discovery pages."""
    headers = {"User-Agent": "OSSOpportunityScout"}
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return cast(bytes, response.read()).decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"Platform fetch failed for {url}: {exc}")
        return ""


def issue_comments(item: Mapping[str, Any], token: str | None) -> list[dict[str, Any]]:
    """Fetch comments for payment verification; basic filters cap threads at 25."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number or not int(item.get("comments") or 0):
        return []
    comments = bounty.github_get(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments?per_page=100",
        token,
    )
    return comments if isinstance(comments, list) else []


SUPPLEMENTAL_CLAIM_PATTERNS = [
    r"\bplanning (?:a |the )?fix\b",
    r"\bplanning to (?:fix|work on|implement|handle)\b",
    r"\bplan to (?:fix|work on|implement|handle)\b",
    r"\bstarting (?:work on|a fix for)\b",
    r"\bi(?:'ll| will) (?:fix|work on|implement|handle)\b",
    r"\bi can take (?:this|it|this one)\b",
    r"\bi(?:'ll| will) take (?:this|it|this one)\b",
    r"\bi(?:'ll| will) take a look at (?:this|it|this one)\b",
    r"\bimplementing (?:this|a fix)\b",
    r"\bworking on (?:a |the )?fix\b",
]
TRIAGE_PENDING_LABELS = {"needs-triage"}
TRIAGE_ACCEPTED_LABELS = {"triage/accepted", "good first issue", "help wanted"}


def linked_open_pr_reason(
    item: Mapping[str, Any], token: str | None, comments: list[dict[str, Any]] | None = None
) -> str | None:
    """Detect explicit implementation PR links in issue comments."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number or not int(item.get("comments") or 0):
        return None

    comments = issue_comments(item, token) if comments is None else comments
    repo_pattern = re.escape(repo)
    candidates = []

    for comment in comments:
        body = str(comment.get("body", ""))
        candidates.extend(
            re.findall(
                rf"https://github\.com/{repo_pattern}/pull/(\d+)",
                body,
                re.IGNORECASE,
            )
        )
        candidates.extend(
            re.findall(
                r"\b(?:related|implementation|opened|submitted)\s+"
                r"(?:pr|pull request)\s*:?\s*#(\d+)\b",
                body,
                re.IGNORECASE,
            )
        )

    for pr_number in dict.fromkeys(candidates):
        pr = bounty.github_get(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
            token,
        )
        if isinstance(pr, dict) and pr.get("state") == "open":
            url = pr.get("html_url") or f"https://github.com/{repo}/pull/{pr_number}"
            return f"existing open implementation PR: {url}"

    return None


def timeline_open_pr_reason(item: Mapping[str, Any], token: str | None) -> str | None:
    """Detect open PRs that GitHub has cross-referenced on the issue timeline."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number:
        return None

    timeline = bounty.github_get(
        f"https://api.github.com/repos/{repo}/issues/{number}/timeline?per_page=100",
        token,
    )
    if not isinstance(timeline, list):
        return None

    for event in timeline:
        if event.get("event") != "cross-referenced":
            continue
        source = event.get("source") or {}
        source_issue = source.get("issue") if isinstance(source, dict) else None
        if not isinstance(source_issue, dict) or not source_issue.get("pull_request"):
            continue
        if source_issue.get("state") != "open":
            continue
        url = source_issue.get("html_url")
        if url:
            return f"existing open implementation PR: {url}"

    return None


def supplemental_claim_reason(
    item: Mapping[str, Any], token: str | None, comments: list[dict[str, Any]] | None = None
) -> str | None:
    """Detect clear work claims not covered by the upstream scanner."""
    if not int(item.get("comments") or 0):
        return None
    comments = issue_comments(item, token) if comments is None else comments

    for comment in comments:
        body = str(comment.get("body", ""))
        for pattern in SUPPLEMENTAL_CLAIM_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                author = (comment.get("user") or {}).get("login", "someone")
                return f"active claim by @{author}"
    return None


def extended_competition_reason(item: Mapping[str, Any], token: str | None) -> str | None:
    """Check search-index PRs, comment-linked PRs, and explicit work claims."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number:
        return "could not identify repository/issue number"

    reason = bounty.has_existing_implementation_pr(repo, number, token)
    if reason:
        return reason

    comments = issue_comments(item, token)
    reason = linked_open_pr_reason(item, token, comments)
    if reason:
        return reason

    for comment in comments:
        body = str(comment.get("body", ""))
        for pattern in bounty.CLAIM_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                author = (comment.get("user") or {}).get("login", "someone")
                return f"active claim by @{author}"

    reason = supplemental_claim_reason(item, token, comments)
    if reason:
        return reason

    return timeline_open_pr_reason(item, token)


def supplemental_payment_signal(item: Mapping[str, Any]) -> str | None:
    """Recognize explicit paid-work wording outside the upstream vocabulary."""
    title, body, labels, _ = issue_text(item)
    text = f"{title}\n{body}"
    term = r"(?:cash\s+prize|stipend|sponsored(?:\s+work)?|funded\s+task)"
    near_amount = re.search(
        term + r".{0,80}?(" + EXTENDED_AMOUNT_RE + r")",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if near_amount:
        return f"explicit paid-work wording: {near_amount.group(1).strip()}"

    amount_near = re.search(
        r"(" + EXTENDED_AMOUNT_RE + r").{0,80}?" + term,
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if amount_near:
        return f"explicit paid-work wording: {amount_near.group(1).strip()}"

    amount = re.search(EXTENDED_AMOUNT_RE, text, re.IGNORECASE)
    if ("bountyhub.dev" in text.lower() or "bountyhub.dev" in labels) and amount:
        return f"named bounty platform + amount (BountyHub): {amount.group(0).strip()}"

    return None


def comment_payment_signal(item: Mapping[str, Any], token: str | None) -> str | None:
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

        if (
            amount
            and ("bountyhub" in login or "bountyhub.dev" in body.lower())
            and re.search(
                r"\bbounty\b.*\bcreated\b|\bcreated\b.*\bbounty\b", body, re.IGNORECASE | re.DOTALL
            )
        ):
            return f"confirmed bounty platform comment (BountyHub): {amount.group(0).strip()}"

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
            return f"explicit /reward comment: ${reward.group(1)}"

        algora = re.search(
            r"/bounty\s+(" + EXTENDED_AMOUNT_RE + r")",
            body,
            re.IGNORECASE,
        )
        if algora:
            return f"explicit /bounty comment: {algora.group(1).strip()}"

    return None


def issue_from_github_url(url: str, token: str | None) -> dict[str, Any] | None:
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


def issuehunt_platform_refs() -> dict[str, str]:
    """Read the official IssueHunt funded-issues pages."""
    refs: dict[str, str] = {}
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
            nearby = page_html[match.end() : match.end() + 1200]
            amount = re.search(r"\$\s*\d[\d,]*(?:\.\d+)?", nearby)
            signal = "confirmed bounty platform feed (IssueHunt)"
            if amount:
                signal += f": {amount.group(0).strip()}"
            refs[source_url] = signal
    return refs


def opire_platform_refs() -> dict[str, str]:
    """Read visible Opire bounty cards and map them back to GitHub issues."""
    refs: dict[str, str] = {}
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

    detail_paths = list(
        dict.fromkeys(
            re.findall(
                r'href=["\'](/issues/[A-Za-z0-9_-]+)["\']',
                normalized,
            )
        )
    )[:PLATFORM_FETCH_LIMIT]

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


def bountyhub_platform_refs() -> dict[str, str]:
    """Read public BountyHub listings when the site exposes them in HTML."""
    refs: dict[str, str] = {}
    listing = fetch_text("https://www.bountyhub.dev/en/bounties")
    if not listing:
        return refs

    normalized = listing.replace("\\/", "/")
    direct = re.findall(
        r"https://github\.com/[^/\s\"'<>]+/[^/\s\"'<>]+/issues/\d+",
        normalized,
    )
    for source_url in direct[:PLATFORM_FETCH_LIMIT]:
        refs[source_url] = "confirmed bounty platform feed (BountyHub)"

    detail_paths = list(
        dict.fromkeys(
            re.findall(
                r'href=["\'](/en/bounty/view/[A-Za-z0-9_-]+)["\']',
                normalized,
            )
        )
    )[:PLATFORM_FETCH_LIMIT]

    for path in detail_paths:
        detail = fetch_text("https://www.bountyhub.dev" + path)
        if not detail:
            continue
        detail = detail.replace("\\/", "/")
        source = re.search(
            r"https://github\.com/[^/\s\"'<>]+/[^/\s\"'<>]+/issues/\d+",
            detail,
        )
        if not source:
            continue
        amount = re.search(EXTENDED_AMOUNT_RE, detail, re.IGNORECASE)
        signal = "confirmed bounty platform feed (BountyHub)"
        if amount:
            signal += f": {amount.group(0).strip()}"
        refs[source.group(0)] = signal

    return refs


def platform_paid_refs() -> dict[str, str]:
    """Collect official-platform discoveries, deduped by source GitHub issue URL."""
    refs: dict[str, str] = {}
    for source in (
        issuehunt_platform_refs(),
        opire_platform_refs(),
        bountyhub_platform_refs(),
    ):
        refs.update(source)
    return refs


def contribution_guide(repo: str, token: str | None) -> str | None:
    for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"):
        data = github_get_optional(
            f"https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path)}",
            token,
        )
        if isinstance(data, dict) and data.get("html_url"):
            return str(data["html_url"])
    return None


def build_candidate(
    item: Mapping[str, Any],
    lane: str,
    signal: str | None,
    repo_meta: Mapping[str, Any],
    guide: str | None,
) -> dict[str, Any]:
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
            cash += (
                20
                if amount >= 500
                else 16
                if amount >= 100
                else 12
                if amount >= 25
                else 8
                if amount >= 5
                else 4
            )
            hourly = amount / effort_hours(effort)
            cash += (
                25
                if hourly >= 100
                else 21
                if hourly >= 50
                else 16
                if hourly >= 20
                else 10
                if hourly >= 10
                else 4
            )
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
        career += 18
        career_reasons.append("10k+ star repo")
    elif stars >= 1000:
        career += 14
        career_reasons.append("1k+ star repo")
    elif stars >= 100:
        career += 9
    elif stars >= 10:
        career += 4
    if active_30d:
        career += 8
        career_reasons.append("repo active in last 30d")
    if repo in TARGET_REPOS:
        career += 14
        career_reasons.append("target repo bonus")

    language = str(repo_meta.get("language") or "Unknown")
    lang = language.lower()
    skill = (
        12
        if lang == "go"
        else 11
        if lang in ("typescript", "javascript")
        else 9
        if lang in ("ruby", "php")
        else 8
        if lang == "hcl"
        else 0
    )
    if skill:
        career_reasons.append(f"{language} codebase")
    infra_terms = (
        "kubernetes",
        "aws",
        "network",
        "dns",
        "proxy",
        "routing",
        "observability",
        "prometheus",
        "otel",
        "distributed",
        "controller",
        "terraform",
        "gitops",
        "backend",
        "api",
        "concurrency",
    )
    if any(term in text for term in infra_terms):
        skill += 6
        career_reasons.append("target infrastructure/domain fit")
    career += min(18, skill)

    depth_terms = (
        "race",
        "deadlock",
        "concurrency",
        "network",
        "dns",
        "proxy",
        "routing",
        "protocol",
        "controller",
        "distributed",
        "storage",
        "performance",
        "memory",
        "leak",
        "api",
    )
    depth = sum(term in text for term in depth_terms)
    career += (
        14
        if depth >= 3
        else 8
        if depth >= 1
        else 4
        if re.search(r"\b(?:test|regression|bug|fix)\b", text)
        else 0
    )
    if depth:
        career_reasons.append("meaningful technical depth")

    issue_points = 0
    if re.search(r"\b(?:test|tests|regression)\b", text):
        issue_points += 6
        career_reasons.append("tests/regression signal")

    maintainer_ready = any(
        label in labels_text
        for label in ("help wanted", "good first issue", "triage/accepted", "refined")
    )
    if maintainer_ready:
        issue_points += 8
        career_reasons.append("maintainer-ready signal")

    if guide:
        issue_points += 3
        career_reasons.append("contribution guide found")

    effort_points = {
        "<1h": 9,
        "1–3h": 7,
        "3–6h": 4,
        "6–12h": 1,
        "1d+": 0,
    }[effort]
    issue_points += effort_points
    if effort in ("<1h", "1–3h"):
        career_reasons.append("bounded implementation scope")

    _, body, _, _ = issue_text(item)
    clarity = 0
    if re.search(r"\b(?:root cause|code path|cause \(from)\b", text):
        clarity += 3
    if "steps to reproduce" in text and "_no response_" not in text:
        clarity += 2
    if re.search(r"\b(?:suggested fix|possible fix|expected behavior)\b", text):
        clarity += 2
    refs = code_reference_count(body)
    clarity += min(3, refs)
    if clarity >= 5:
        career_reasons.append("clear implementation/reproduction detail")
    elif clarity:
        career_reasons.append("implementation detail available")
    issue_points += min(10, clarity)

    career += min(30, issue_points)
    career -= {"none": 0, "low": 2, "medium": 6, "high": 12}[comp]
    if effort == "6–12h":
        career -= 3
        career_reasons.append("broader implementation scope")
    elif effort == "1d+":
        career -= 10
        career_reasons.append("large-scope penalty")

    created = bounty.parse_github_datetime(item.get("created_at"))
    if lane == "strategic" and created and not maintainer_ready:
        age_days = max(0, (datetime.now(timezone.utc) - created).days)
        if age_days > 730:
            career -= 15
            career_reasons.append("stale backlog age penalty")
        elif age_days > 365:
            career -= 8
            career_reasons.append("older backlog age penalty")

    career = max(0, min(100, career))

    priority = (
        career
        if lane == "strategic"
        else min(100, max(cash, career) + (5 if cash >= 70 and career >= 70 else 0))
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
        "career_reasons": career_reasons[:10],
        "contribution_guide": guide,
        "comments": int(item.get("comments") or 0),
        "updated_at": item.get("updated_at"),
        "rejection_reason": None,
    }


def refresh_issue(
    item: Mapping[str, Any], token: str | None
) -> tuple[dict[str, Any] | None, str | None]:
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


def upstream_wrapper_issue_url(item: Mapping[str, Any]) -> str | None:
    """Return the real GitHub source issue for explicit aggregator/handoff wrappers."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    if "ORIGINAL_ISSUE_URL" not in body:
        return None
    if "TARGET_REPOSITORY" not in body and "READY FOR ENGINEERING" not in title.upper():
        return None

    match = re.search(
        r"\bORIGINAL_ISSUE_URL\b.{0,240}?"
        r"(https://github\.com/[^/\s]+/[^/\s]+/issues/\d+)",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else None


def non_actionable_diagnostic_reason(item: Mapping[str, Any]) -> str | None:
    """Reject machine/OS crash diagnostics that lack an actionable contributor path."""
    _, body, labels, text = issue_text(item)
    maintainer_ready = any(label in labels for label in TRIAGE_ACCEPTED_LABELS)
    if maintainer_ready or code_reference_count(body):
        return None

    system_failure = re.search(
        r"\b(?:hard hang|black screen|kernel panic|force power|force-reset|force reset)\b",
        text,
    )
    hardware_specific = re.search(
        r"\bmacos\b.*\b(?:m[1-9]|apple silicon)\b|"
        r"\b(?:m[1-9]|apple silicon)\b.*\bmacos\b",
        text,
        re.DOTALL,
    )
    diagnostic_only = re.search(
        r"\b(?:no deterministic repro|no .{0,30}(?:app|userspace) crash|"
        r"not in the panic backtrace|sysdiagnose|panic logs?|filed with apple|"
        r"force-reset reports?)\b",
        text,
        re.DOTALL,
    )
    if system_failure and hardware_specific and diagnostic_only:
        return "hardware/kernel diagnostic report without actionable contributor scope"
    return None


def strategic_rejection(item: Mapping[str, Any], token: str | None) -> str | None:
    if not bounty.is_clean_candidate(item):
        return "failed basic eligibility filter"
    _, _, labels, text = issue_text(item)
    if "oss opportunity queue" in text:
        return "generated opportunity-scout report"
    if any(
        x in labels
        for x in (
            "question",
            "support",
            "needs info",
            "needs-info",
            "needs-information",
            "waiting for info",
            "waiting-for-info",
            "invalid",
        )
    ):
        return "support/triage issue rather than a contributor task"

    label_set = {
        (str(label.get("name", "")) if isinstance(label, dict) else str(label)).strip().lower()
        for label in (item.get("labels") or [])
        if (str(label.get("name", "")) if isinstance(label, dict) else str(label)).strip()
    }
    if TRIAGE_PENDING_LABELS & label_set and not TRIAGE_ACCEPTED_LABELS & label_set:
        return "awaiting maintainer triage"

    diagnostic_reason = non_actionable_diagnostic_reason(item)
    if diagnostic_reason:
        return diagnostic_reason

    return extended_competition_reason(item, token)


def verify(
    item: Mapping[str, Any],
    token: str | None,
    repo_cache: dict[str, dict[str, Any]],
    guide_cache: dict[str, str | None],
    require_paid: bool = False,
    payment_signal_override: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    fresh, reason = refresh_issue(item, token)
    if reason:
        return None, reason
    if fresh is None:
        return None, "could not refresh source issue"

    upstream_url = upstream_wrapper_issue_url(fresh)
    if upstream_url:
        upstream = issue_from_github_url(upstream_url, token)
        if upstream is None:
            return None, "could not refresh upstream issue from aggregator wrapper"
        fresh, reason = refresh_issue(upstream, token)
        if reason:
            return None, f"upstream source: {reason}"
        if fresh is None:
            return None, "could not refresh upstream issue from aggregator wrapper"

    if not bounty.is_clean_candidate(fresh):
        return None, "failed basic eligibility filter after source refresh"

    issue_signal = bounty.payment_signal(fresh) or supplemental_payment_signal(fresh)
    comment_signal = None
    if not issue_signal and int(fresh.get("comments") or 0):
        comment_signal = comment_payment_signal(fresh, token)
    signal = issue_signal or comment_signal or payment_signal_override

    if require_paid or signal:
        reason, verified_issue_signal = bounty.candidate_rejection_reason(
            fresh,
            token,
        )
        if reason and reason != "no explicit payment signal":
            return None, reason

        if verified_issue_signal:
            signal = verified_issue_signal

        if not signal:
            return None, "no explicit payment signal"

        # The upstream verifier stops before competition checks when payment is
        # only present in comments/platform feeds, so finish those checks here.
        if reason == "no explicit payment signal":
            repo, number = bounty.issue_repo_and_number(fresh)
            competition_reason = extended_competition_reason(fresh, token)
            if competition_reason:
                return None, competition_reason

        lane = "paid"
    else:
        reason = strategic_rejection(fresh, token)
        if reason:
            return None, reason
        lane = "strategic"

    repo, _ = bounty.issue_repo_and_number(fresh)
    if repo is None:
        return None, "could not identify repository/issue number"
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


def add_reject(
    counts: dict[str, int], examples: list[dict[str, Any]], item: Mapping[str, Any], reason: str
) -> None:
    counts[reason] = counts.get(reason, 0) + 1
    if len(examples) < 12:
        examples.append({"url": item.get("html_url"), "title": item.get("title"), "reason": reason})


def discover_paid(
    token: str | None,
    seen: set[str],
    repo_cache: dict[str, dict[str, Any]],
    guide_cache: dict[str, str | None],
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    found: list[dict[str, Any]] = []
    touched: set[str] = set()
    rejected: dict[str, int] = {}
    examples: list[dict[str, Any]] = []

    for query in PAID_DISCOVERY_QUERIES:
        for item in bounty.search_github(query, token).get("items", []):
            url = item.get("html_url")
            if not url or url in seen or url in touched:
                continue
            touched.add(url)
            if not bounty.is_clean_candidate(item):
                continue
            candidate, reason = verify(
                item,
                token,
                repo_cache,
                guide_cache,
                require_paid=True,
            )
            if reason:
                add_reject(rejected, examples, item, reason)
                print(f"Skipping paid candidate {url}: {reason}")
            else:
                assert candidate is not None
                found.append(candidate)

    # Official platform feeds can expose funded issues that contain no bounty
    # keywords on GitHub at all. Source GitHub issue still gets final authority.
    for source_url, platform_signal in platform_paid_refs().items():
        if source_url in seen or source_url in touched:
            continue
        touched.add(source_url)
        item = issue_from_github_url(source_url, token)
        if not item:
            continue
        if not bounty.is_clean_candidate(item):
            continue

        candidate, reason = verify(
            item,
            token,
            repo_cache,
            guide_cache,
            require_paid=True,
            payment_signal_override=platform_signal,
        )
        if reason:
            add_reject(rejected, examples, item, reason)
            print(f"Skipping platform candidate {source_url}: {reason}")
        else:
            assert candidate is not None
            found.append(candidate)

    return found, rejected, examples


def strategic_verification_items(
    provisional: list[tuple[int, int, int, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Build a high-scoring but repo-diverse shortlist for expensive verification."""
    ordered = sorted(provisional, key=lambda row: row[:3], reverse=True)
    selected: list[dict[str, Any]] = []
    per_repo: dict[str, int] = {}

    for _, _, _, item in ordered:
        repo, _ = bounty.issue_repo_and_number(item)
        if not repo:
            continue
        if per_repo.get(repo, 0) >= STRATEGIC_VERIFY_PER_REPO:
            continue

        selected.append(item)
        per_repo[repo] = per_repo.get(repo, 0) + 1
        if len(selected) >= STRATEGIC_VERIFY_LIMIT:
            break

    return selected


def discover_strategic(
    token: str | None,
    seen: set[str],
    paid_urls: set[str],
    repo_cache: dict[str, dict[str, Any]],
    guide_cache: dict[str, str | None],
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    provisional: list[tuple[int, int, int, dict[str, Any]]] = []
    touched: set[str] = set()
    rejected: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    for query in target_repo_queries() + STRATEGIC_GLOBAL_QUERIES:
        for item in bounty.search_github(query, token, per_page=STRATEGIC_SEARCH_PER_PAGE).get(
            "items", []
        ):
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
            provisional.append(
                (preview["priority_score"], preview["career_score"], preview["cash_score"], item)
            )

    found: list[dict[str, Any]] = []
    for item in strategic_verification_items(provisional):
        candidate, reason = verify(item, token, repo_cache, guide_cache)
        if reason:
            add_reject(rejected, examples, item, reason)
            print(f"Skipping strategic candidate {item.get('html_url')}: {reason}")
            continue

        assert candidate is not None
        if candidate["career_score"] < STRATEGIC_MIN_CAREER_SCORE:
            reason = (
                f"career score {candidate['career_score']}/100 below strategic threshold "
                f"{STRATEGIC_MIN_CAREER_SCORE}/100"
            )
            add_reject(rejected, examples, item, reason)
            print(f"Skipping strategic candidate {item.get('html_url')}: {reason}")
            continue

        found.append(candidate)
    return found, rejected, examples


def github_report_ref(text: Any) -> str:
    """Make GitHub issue/PR URLs clickable without creating backlinks."""
    value = str(text or "")
    return re.sub(
        r"https://github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)",
        lambda m: (
            f"https://redirect.github.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/{m.group(4)}"
        ),
        value,
        flags=re.IGNORECASE,
    )


def markdown_candidate(candidate: Mapping[str, Any], idx: int) -> str:
    hourly = (
        f"~${candidate['expected_hourly']:.0f}/h"
        if candidate["expected_hourly"] is not None
        else "unknown / not USD-comparable"
    )
    guide = (
        f"[contribution guide]({candidate['contribution_guide']})"
        if candidate["contribution_guide"]
        else "not found at common paths"
    )
    lines = [
        f"#### {idx}. [{candidate['repo']} #{candidate['issue_number']}]({github_report_ref(candidate['url'])}): "
        f"{github_report_ref(candidate['title'])}",
    ]
    if candidate["paid"]:
        lines.extend(
            [
                f"- **Reward:** {candidate['reward'] or 'unknown'}",
                f"- **Payment confidence:** {candidate['payment_confidence']}/100",
                f"- **Cash score:** {candidate['cash_score']}/100",
                f"- **Effort:** {candidate['effort']}",
                f"- **Expected hourly value:** {hourly}",
            ]
        )
    else:
        lines.extend(
            [
                f"- **Career score:** {candidate['career_score']}/100",
                f"- **Effort:** {candidate['effort']}",
            ]
        )

    lines.extend(
        [
            f"- **Competition:** {candidate['competition']}",
            f"- **Repo stars:** {candidate['stars']}",
            f"- **Repo recent activity:** {candidate['recent_activity']}",
            f"- **Language:** {candidate['language']}",
            f"- **Labels:** {', '.join(candidate['labels']) or 'none'}",
            f"- **Contribution process:** {guide}",
        ]
    )
    if candidate["paid"]:
        lines.append(f"- **Cash reasons:** {', '.join(candidate['cash_reasons'])}")
    else:
        lines.append(f"- **Career reasons:** {', '.join(candidate['career_reasons'])}")
    return "\n".join(lines) + "\n\n"


def notification_candidate(candidate: Mapping[str, Any], idx: int) -> list[str]:
    title = str(candidate["title"] or "")
    if len(title) > 100:
        title = title[:97] + "..."
    lines = [f"{idx}. *{candidate['repo']} #{candidate['issue_number']}* — {title}"]
    if candidate["paid"]:
        lines.append(
            f"   • paid bounty | reward: {candidate['reward'] or 'unknown'} | "
            f"cash: {candidate['cash_score']}/100"
        )
    else:
        lines.append(f"   • strategic OSS | career: {candidate['career_score']}/100")
    lines.extend(
        [
            f"   • {candidate['effort']} | competition: {candidate['competition']}",
            f"   • {candidate['url']}",
        ]
    )
    return lines


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo_fullname = os.environ.get("GITHUB_REPOSITORY")
    seen = bounty.load_seen_bounties()
    repo_cache: dict[str, dict[str, Any]] = {}
    guide_cache: dict[str, str | None] = {}

    paid, paid_rejects, paid_examples = discover_paid(token, seen, repo_cache, guide_cache)
    strategic, strategic_rejects, strategic_examples = discover_strategic(
        token, seen, {x["url"] for x in paid}, repo_cache, guide_cache
    )

    by_url: dict[str, dict[str, Any]] = {}
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
        delivered = (
            bounty.send_telegram_notification(telegram_token, telegram_chat_id, message)
            or delivered
        )
    if discord_webhook:
        attempted = True
        delivered = (
            bounty.send_discord_notification(discord_webhook, message.replace("•", "-"))
            or delivered
        )

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
                source = github_report_ref(item["url"])
                title = github_report_ref(item["title"] or source)
                reason = github_report_ref(item["reason"])
                body += f"- [{title}]({source}): {reason}\n"
        delivered = (
            bounty.create_github_issue(
                repo_fullname,
                token,
                f"🎯 OSS Opportunity Queue: {len(queue)} new verified candidate{'s' if len(queue) != 1 else ''}",
                body,
            )
            or delivered
        )

    rejects = dict(paid_rejects)
    for reason, count in strategic_rejects.items():
        rejects[reason] = rejects.get(reason, 0) + count
    if rejects:
        print(
            "Filtered verified candidates: "
            + ", ".join(f"{k}={v}" for k, v in sorted(rejects.items()))
        )

    if attempted and delivered:
        seen.update(x["url"] for x in queue)
        bounty.save_seen_bounties(seen)
    else:
        print("No notification was delivered; state was not updated.")


if __name__ == "__main__":
    main()
