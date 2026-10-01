from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
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
    'is:issue is:open no:assignee label:"help wanted" label:"bug" sort:updated-desc',
    'is:issue is:open no:assignee label:"good first issue" label:"bug" sort:updated-desc',
    'is:issue is:open no:assignee label:"help wanted" regression sort:updated-desc',
]
STRATEGIC_SEARCH_PER_PAGE = 20
TARGET_REPO_FETCH_PER_PAGE = 50
TARGET_REPO_FETCH_PAGES = 3
STRATEGIC_INSPECT_PER_REPO = 15
STRATEGIC_KEEP_PER_REPO = 3
STRATEGIC_MIN_CAREER_SCORE = 55
STRATEGIC_AUDIT_LIMIT = 20
PAID_MIN_CASH_SCORE = 55
REPORT_LIMIT = 8
NETWORK_WORKERS = 6
CACHE_LOCK = Lock()
CACHE_KEY_LOCKS: dict[str, Any] = {}


def cache_lock_for(key: str) -> Any:
    """Serialize cache fills per repository without blocking unrelated repositories."""
    with CACHE_LOCK:
        return CACHE_KEY_LOCKS.setdefault(key, Lock())


PAID_DISCOVERY_QUERIES = [
    "is:issue is:open bounty in:title,body sort:updated-desc",
    'is:issue is:open "/reward" in:comments sort:updated-desc',
    "is:issue is:open (opire.dev OR bountyhub.dev OR algora.io) in:comments sort:updated-desc",
]
EXTENDED_AMOUNT_RE = (
    r"(?:[$€£¥₹]\s*\d[\d,]*(?:\.\d+)?|"
    r"(?<![A-Za-z])R\s*\d[\d,]*(?:\.\d+)?|"
    r"\d+(?:\.\d+)?\s*(?:usd|usdc|usdt|eur|gbp|cad|aud|nzd|jpy|chf|"
    r"inr|zar|dai|xmr|sol|eth|btc)\b)"
)
PLATFORM_FETCH_LIMIT = 20
ISSUEHUNT_PAGES = 2
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def target_repo_issue_pool(repo: str, token: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch enough real issues even though GitHub mixes PRs into /issues."""
    issues: list[dict[str, Any]] = []

    for page in range(1, TARGET_REPO_FETCH_PAGES + 1):
        params = urllib.parse.urlencode(
            {
                "state": "open",
                "sort": "updated",
                "direction": "desc",
                "per_page": TARGET_REPO_FETCH_PER_PAGE,
                "page": page,
            }
        )
        data = bounty.github_get(
            f"https://api.github.com/repos/{repo}/issues?{params}",
            token,
        )
        if not isinstance(data, list):
            error = f"target repo discovery failed for {repo}; scan coverage incomplete"
            return issues[:STRATEGIC_SEARCH_PER_PAGE], error

        for item in data:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            issues.append(item)
            if len(issues) >= STRATEGIC_SEARCH_PER_PAGE:
                return issues[:STRATEGIC_SEARCH_PER_PAGE], None

        if len(data) < TARGET_REPO_FETCH_PER_PAGE:
            break

    return issues[:STRATEGIC_SEARCH_PER_PAGE], None


def maintainer_ready_signal(labels_text: str) -> bool:
    """Recognize common contributor-ready label dialects."""
    normalized = re.sub(r"[-_]+", " ", labels_text.lower())
    return any(
        marker in normalized
        for marker in (
            "help wanted",
            "good first issue",
            "triage/accepted",
            "refined",
        )
    )


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


def strategic_basic_candidate(item: Mapping[str, Any]) -> bool:
    """Apply strategic eligibility without making comment volume disqualifying."""
    if bounty.is_clean_candidate(item):
        return True
    if int(item.get("comments") or 0) <= bounty.MAX_COMMENTS:
        return False

    relaxed = dict(item)
    relaxed["comments"] = bounty.MAX_COMMENTS
    return bounty.is_clean_candidate(relaxed)


def documentation_microfix(item: Mapping[str, Any]) -> bool:
    """Detect tiny docs-only edits that should not outrank substantive code work."""
    title, body, labels, text = issue_text(item)
    docs_signal = bool(
        re.search(
            r"\b(?:docs?|documentation|readme|typo|spelling|broken image|broken link)\b",
            f"{title}\n{labels}",
            re.IGNORECASE,
        )
        or re.search(r"\b(?:docs?/|readme(?:\.[a-z]+)?\b)", body, re.IGNORECASE)
    )
    micro_signal = bool(
        re.search(
            r"\b(?:typo|spelling|broken image|broken link|link fix|image link|"
            r"documentation cleanup|docs cleanup)\b",
            text,
            re.IGNORECASE,
        )
    )
    return docs_signal and micro_signal and code_reference_count(body) == 0


def estimate_effort(item: Mapping[str, Any]) -> str:
    title, body, labels, text = issue_text(item)
    comments = int(item.get("comments") or 0)
    file_refs = code_reference_count(body)

    if (
        "kind/feature" in labels
        or "feature request" in labels
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
        or re.search(
            r"\b(?:unable to reproduce|cannot reproduce|can't reproduce|"
            r"haven't been able to reproduce|have not been able to reproduce|"
            r"low-probability race|non[- ]deterministic repro)\b",
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
        or "enhancement" in labels
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
    """Fetch issue comments for activity, payment, and competition checks."""
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

STRATEGIC_CLAIM_MAX_AGE_DAYS = 365


def normalized_claim_text(text: str) -> str:
    """Normalize apostrophes so common contractions share the same match path."""
    return text.replace("’", "'").replace("‘", "'")


def explicit_ownership_claim(text: str) -> bool:
    """Recognize first-person statements that take ownership of implementation."""
    patterns = (
        r"\bi(?:'d| would) like to (?:work on|take|handle|implement|fix|resolve)\b",
        r"\bi(?:'m| am) interested in working on\b",
        r"\bi(?:'m| am) (?:taking|working on) (?:this|it|an independent pass)\b",
        r"\bi can (?:take|work on|handle|implement|fix|resolve)\b",
        r"\bi(?:'ll| will) (?:take|work on|handle|implement|fix|resolve)\b",
        r"\bi(?:'ll| will) take a look at implementing\b",
        r"\bbefore i (?:write|start writing) code\b",
        r"\b(?:please|kindly) assign(?: it| this issue)? to me\b",
        r"\bassign (?:this|it) to me\b",
        r"/attempt\b",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def implementation_underway_claim(text: str) -> bool:
    """Recognize first-person evidence that implementation already exists or is underway."""
    patterns = (
        r"\bi(?:'ve| have) implemented\b",
        r"\bi(?:'ve got| have(?: got)?) (?:a |the )?(?:fix|patch)\b",
        r"\bi(?:'ve| have) added (?:unit |e2e |regression )?tests?\b",
        r"\bi(?:'ve| have) tests? ready\b",
        r"\bi(?:'m| am) (?:currently )?implementing\b",
        r"\bi(?:'m| am) working on (?:a |the )?fix\b",
        r"^\s*(?:currently implementing|working on (?:a |the )?fix)\b",
        r"^\s*planning (?:a |the )?fix\b",
        r"^\s*(?:planning|plan) to (?:fix|work on|implement|handle)\b",
        r"^\s*starting (?:work on|a fix for)\b",
        r"^\s*delivered in pr\b",
        r"^\s*submitted (?:a )?pr\b",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def pr_intent_claim(text: str) -> bool:
    """Recognize language that says the author intends to submit implementation work."""
    patterns = (
        r"\bi(?:'ll| will) open (?:a |the )?(?:pr|pull request)\b",
        r"\bi can (?:send|open|submit) (?:a |the )?(?:pr|pull request)\b",
        r"\bbefore i (?:open|submit) (?:a |the )?(?:pr|pull request)\b",
        r"\bbefore submitting (?:a |the )?(?:pr|pull request)\b",
        r"\bi(?:'m| am) preparing (?:a |the )?(?:pr|pull request)\b",
        r"^\s*preparing (?:a |the )?(?:pr|pull request)\b",
        r"\bwould (?:the )?(?:team|maintainers|you) welcome (?:a |the )?(?:pr|pull request)\b",
        r"\bcan i submit (?:this|it|(?:a |the )?(?:pr|pull request))\b",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def concrete_first_person_plan_claim(text: str) -> bool:
    """Recognize a concrete implementation plan only when the author owns the work."""
    return bool(
        re.search(r"\bmy plan is to\b", text, re.IGNORECASE)
        or re.search(
            r"\bi(?:'m| am) going to "
            r"(?:add|change|modify|update|implement|fix|refactor|write|remove|move|introduce)\b",
            text,
            re.IGNORECASE,
        )
    )


def strategic_claim_text(text: str) -> bool:
    """Return True only for language that clearly claims or performs implementation work."""
    normalized = normalized_claim_text(text)
    return (
        explicit_ownership_claim(normalized)
        or implementation_underway_claim(normalized)
        or pr_intent_claim(normalized)
        or concrete_first_person_plan_claim(normalized)
    )


def claim_source_is_recent(source: Mapping[str, Any], *, issue_body: bool = False) -> bool:
    """Keep old claims from permanently suppressing strategic opportunities."""
    timestamp = (
        source.get("created_at")
        if issue_body
        else (source.get("updated_at") or source.get("created_at"))
    )
    stamp = bounty.parse_github_datetime(timestamp)
    if stamp is None:
        return True
    age_days = max(0, (datetime.now(timezone.utc) - stamp).days)
    return age_days <= STRATEGIC_CLAIM_MAX_AGE_DAYS


def strategic_claim_reason(
    item: Mapping[str, Any],
    comments: list[dict[str, Any]],
) -> str | None:
    """Detect active implementation ownership in the issue body and recent comments."""
    body = str(item.get("body", ""))
    if body and claim_source_is_recent(item, issue_body=True) and strategic_claim_text(body):
        return "issue author already has an implementation/fix in progress"

    for comment in comments:
        if not claim_source_is_recent(comment):
            continue
        body = str(comment.get("body", ""))
        if strategic_claim_text(body):
            author = (comment.get("user") or {}).get("login", "someone")
            return f"active claim by @{author}"
    return None


def triage_pending_signal(labels_text: str) -> bool:
    """Recognize pending-triage label dialects used by target repositories."""
    normalized = re.sub(r"[-_/:]+", " ", labels_text.lower())
    return any(
        marker in normalized
        for marker in (
            "needs triage",
            "triage pending",
            "bug possible",
            "needs analysis",
            "needs investigation",
        )
    )


def issue_label_set(item: Mapping[str, Any]) -> set[str]:
    """Return normalized raw label names without flattening their separators."""
    return {
        (str(label.get("name", "")) if isinstance(label, dict) else str(label)).strip().lower()
        for label in (item.get("labels") or [])
        if (str(label.get("name", "")) if isinstance(label, dict) else str(label)).strip()
    }


def proposal_stage_signal(item: Mapping[str, Any]) -> bool:
    """Recognize explicit proposal/RFC/discussion-stage metadata."""
    title, _, labels, _ = issue_text(item)
    normalized_labels = re.sub(r"[-_/:]+", " ", labels)
    return bool(
        re.search(r"\b(?:proposal|rfc)\b", title, re.IGNORECASE)
        or any(
            marker in normalized_labels
            for marker in (
                "kind proposal",
                "type proposal",
                "status proposal",
                "rfc",
                "needs discussion",
                "discussion",
            )
        )
    )


def maintainer_comment_authority(comment: Mapping[str, Any]) -> bool:
    """Trust maintainer associations plus explicit project-action comments."""
    association = str(comment.get("author_association", "")).upper()
    if association in TRUSTED_ASSOCIATIONS:
        return True
    if association != "CONTRIBUTOR":
        return False

    body = str(comment.get("body", "")).lower()
    return any(
        marker in body
        for marker in (
            "hand it off to the engineering team",
            "hand this off to the engineering team",
            "we're marking this as",
            "we are marking this as",
            "we'll reevaluate",
            "we will reevaluate",
            "i'm closing this",
            "i am closing this",
            "we're closing this",
            "we are closing this",
        )
    )


def maintainer_readiness_comment_state(
    item: Mapping[str, Any],
    comments: list[dict[str, Any]] | None,
) -> tuple[bool | None, str | None]:
    """Return the latest explicit trusted-maintainer readiness stance."""
    proposal_stage = proposal_stage_signal(item)
    state: bool | None = None
    reason: str | None = None
    diagnostic_pending = False

    for comment in comments or []:
        body = normalized_claim_text(str(comment.get("body", ""))).lower()

        supplied_diagnostic = bool(
            re.search(
                r"\b(?:attached|provided|uploaded|included|here(?:'s| is)|see)\b"
                r".{0,160}\b(?:cpu profile|memory profile|heap profile|profile|stack trace|"
                r"minimal reproducer|reproducer|logs?|benchmark|trace|dump)\b",
                body,
                re.DOTALL,
            )
        )
        if diagnostic_pending and supplied_diagnostic:
            state = None
            reason = None
            diagnostic_pending = False

        if not maintainer_comment_authority(comment):
            continue

        ready = any(
            marker in body
            for marker in (
                "ready for implementation",
                "ready to implement",
                "feel free to work on this",
                "contributions welcome",
                "prs welcome",
                "pull requests welcome",
                "go ahead and implement",
                "you can start implementation",
                "happy to accept a pr",
                "happy to accept a pull request",
                "pr in this repository is welcome",
                "pull request in this repository is welcome",
                "implementation is wanted here",
                "reviving this issue",
                "revive this issue",
                "this issue is active again",
                "reopening this for implementation",
            )
        )
        if ready:
            state = True
            reason = None
            diagnostic_pending = False
            continue

        requested_diagnostic = bool(
            re.search(
                r"\b(?:could|can|would)\s+you\s+(?:please\s+)?"
                r"(?:provide|share|attach|capture|collect|send)\b|"
                r"\bwould\s+it\s+be\s+possible\s+to\s+(?:provide|share|attach)\b|"
                r"\bplease\s+(?:provide|share|attach|capture|collect|send)\b|"
                r"\bwe\s+need\s+(?:a|the)\b",
                body,
            )
            and re.search(
                r"\b(?:cpu|memory|heap)\s+profiles?\b|"
                r"\bprofiles?\s+from\b|"
                r"\bstack traces?\b|"
                r"\bminimal reproduc(?:er|tion)\b|"
                r"\breproducers?\b|"
                r"\blogs?\s+(?:from|required|showing)\b|"
                r"\bbenchmarks?\b|"
                r"\btraces?\b|"
                r"\bdumps?\b",
                body,
            )
        )
        redirect_target = bool(
            re.search(
                r"https://github\.com/[\w.-]+/[\w.-]+|"
                r"(?<![\w.-])[\w.-]+/[\w.-]+(?![\w.-])|"
                r"\b(?:specification|upstream|another repository|another project|"
                r"canonical repository)\b",
                body,
            )
        )
        cross_project_redirect = redirect_target and bool(
            re.search(
                r"\brequires?\s+(?:a\s+)?(?:change|fix)\s+in\b|"
                r"\bneeds?\s+to\s+be\s+(?:fixed|changed|implemented)\s+in\b|"
                r"\bplease\s+open\s+(?:a\s+)?(?:ticket|issue)\s+(?:there|against)\b|"
                r"\bbest\s+to\s+open\s+(?:a\s+)?(?:ticket|issue)\s+there\b|"
                r"\b(?:implementation|change|fix)\s+belongs\s+in\b|"
                r"\bthis\s+is\s+an?\s+upstream\s+issue\b",
                body,
            )
        )
        canonical_reference = bool(
            re.search(
                r"(?<!\w)#\d+\b|https://github\.com/[\w.-]+/[\w.-]+/issues/\d+",
                body,
            )
        )
        canonical_duplicate = (
            canonical_reference
            and "not a duplicate" not in body
            and bool(
                re.search(
                    r"\blooks?\s+like\s+(?:a\s+)?(?:\(?possible\)?\s+)?"
                    r"duplicate\s+of\b|"
                    r"\bpossible\s+duplicate\s+of\b|"
                    r"\btracked\s+in\s+(?:issue\s+)?"
                    r"(?:#|https://github\.com/)|"
                    r"\bdiscussion\s+is\s+(?:being\s+)?tracked\s+there\b|"
                    r"\bplease\s+continue\s+(?:this|the discussion)\s+in\b",
                    body,
                )
            )
        )

        hold_reason: str | None = None
        if requested_diagnostic:
            hold_reason = "maintainer is waiting for requested diagnostic evidence"
        elif cross_project_redirect:
            hold_reason = "maintainer redirected implementation/discussion to another project"
        elif canonical_duplicate:
            hold_reason = "maintainer indicates this is probably tracked by another canonical issue"
        elif any(
            marker in body
            for marker in (
                "needs discussion",
                "need more discussion",
                "need to discuss this first",
                "should discuss this first",
            )
        ):
            hold_reason = "maintainer says issue still needs discussion"
        elif any(
            marker in body
            for marker in (
                "needs investigation",
                "need more investigation",
                "need to investigate",
                "needs engineering investigation",
            )
        ):
            hold_reason = "maintainer says issue still needs investigation"
        elif any(
            marker in body
            for marker in (
                "needs reproduction",
                "need a reproduction",
                "need reproduction",
                "please reproduce",
                "can you reproduce",
            )
        ) or ("are you sure" in body and "reproduc" in body):
            hold_reason = "maintainer says reproduction is still required"
        elif any(
            marker in body
            for marker in (
                "not ready for implementation",
                "not ready to implement",
                "please wait before implementing",
                "please wait to implement",
                "hold off on implementation",
                "hold off implementing",
                "do not start implementation",
                "don't start implementation",
            )
        ):
            hold_reason = "maintainer asked contributors to wait before implementation"
        elif any(
            marker in body
            for marker in (
                "needs clarification",
                "need clarification",
                "need to clarify",
                "please clarify before",
            )
        ):
            hold_reason = "maintainer says issue still needs clarification"
        elif proposal_stage and any(
            marker in body
            for marker in (
                "gauge community interest",
                "gather feedback",
                "collect feedback",
                "community time to weigh in",
                "reevaluate based on the feedback",
                "re-evaluate based on the feedback",
                "before committing",
            )
        ):
            hold_reason = "proposal is still gathering feedback"
        elif proposal_stage and "time-boxed" in body and "discussion" in body:
            hold_reason = "proposal is still gathering feedback"

        if hold_reason:
            state = False
            reason = hold_reason
            diagnostic_pending = requested_diagnostic

    return state, reason

def readiness_pending_label_reason(
    item: Mapping[str, Any],
    ready_override: bool = False,
) -> str | None:
    """Reject explicit not-ready label states unless readiness is overridden."""
    if ready_override:
        return None

    normalized = [re.sub(r"[-_/:]+", " ", label) for label in issue_label_set(item)]
    rules = (
        ("needs reproduction", "awaiting reproduction confirmation"),
        ("waiting for reproduction", "awaiting reproduction confirmation"),
        ("needs discussion", "awaiting maintainer discussion"),
        ("needs investigation", "awaiting maintainer investigation"),
        ("needs analysis", "awaiting maintainer investigation"),
        ("needs clarification", "awaiting maintainer clarification"),
        ("needs design", "awaiting maintainer design decision"),
    )
    for marker, reason in rules:
        if any(marker in label for label in normalized):
            return reason
    return None



def abandoned_lifecycle_reason(
    item: Mapping[str, Any],
    ready_override: bool = False,
) -> str | None:
    """Reject unambiguously abandoned lifecycle states unless explicitly revived."""
    if ready_override:
        return None
    if "lifecycle/rotten" in issue_label_set(item):
        return "issue is in an abandoned/rotten lifecycle state"
    return None

def automated_tracking_issue_reason(item: Mapping[str, Any]) -> str | None:
    """Reject bot-maintained dashboards/trackers that are not contributor tasks."""
    title, body, labels, _ = issue_text(item)
    login = str((item.get("user") or {}).get("login", "")).lower()
    bot_authored = login.endswith("[bot]") or any(
        marker in login for marker in ("renovate", "dependabot")
    )
    if not bot_authored:
        return None

    title_text = title.lower()
    dependency_tracking = bool(
        re.search(
            r"\b(?:dependency|dependencies)\s+(?:dashboard|tracker|tracking)\b",
            title_text,
        )
    )
    renovate_dashboard = (
        "renovate" in f"{login}\n{body}".lower()
        and "dependencies" in labels
        and "dashboard" in title_text
    )
    if dependency_tracking or renovate_dashboard:
        return "automated dependency dashboard, not an implementation task"
    return None


def release_tracking_reason(
    item: Mapping[str, Any],
    comments: list[dict[str, Any]] | None = None,
) -> str | None:
    """Reject release bookkeeping and work that is already implemented."""
    title, body, _, _ = issue_text(item)
    tracking_text = title.lower()
    if re.search(
        r"\brelease(?:\s+\S+){0,2}\s+(?:tracking|tracker|checklist|planning)\b|"
        r"\b(?:tracking|tracker|checklist)\s+(?:for\s+)?release\b",
        tracking_text,
    ):
        return "release planning/tracking issue, not implementation work"

    comment_text = "\n".join(str(comment.get("body", "")) for comment in comments or [])
    evidence = f"{body}\n{comment_text}".lower()
    implementation_done = any(
        re.search(pattern, evidence, re.IGNORECASE | re.DOTALL)
        for pattern in (
            r"\bpr\s*#\d+.{0,120}\bmerged\b",
            r"\balready\s+(?:merged|fixed|implemented|resolved)\b",
            r"\b(?:seems|appears)\s+to\s+be\s+resolved\s+by\b",
            r"\b(?:is|was)\s+resolved\s+by\b",
        )
    )
    release_only = any(
        re.search(pattern, evidence, re.IGNORECASE | re.DOTALL)
        for pattern in (
            r"\brelease\s+tag\s+has\s+not\s+yet\s+been\s+published\b",
            r"\brequest\s*:\s*please\s+tag\s+(?:a\s+)?(?:new\s+)?release\b",
            r"\bplease\s+(?:tag|cut|publish)\s+(?:a\s+)?(?:new\s+)?release\b",
            r"\bwaiting\s+for\s+(?:a\s+)?(?:release|tag)\b",
            r"\bonly\s+(?:release|tagging)\s+remains\b",
        )
    )
    if implementation_done and release_only:
        return "implementation already merged; only release/tagging remains"
    return None


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


def extended_competition_reason(
    item: Mapping[str, Any],
    token: str | None,
    comments: list[dict[str, Any]] | None = None,
) -> str | None:
    """Check search-index PRs, comment-linked PRs, and explicit work claims."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number:
        return "could not identify repository/issue number"

    reason = bounty.has_existing_implementation_pr(repo, number, token)
    if reason:
        return reason

    comments = issue_comments(item, token) if comments is None else comments
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

    return None


def strategic_competition_reason(
    item: Mapping[str, Any],
    token: str | None,
    comments: list[dict[str, Any]] | None = None,
) -> str | None:
    """Apply strategic-only competition checks without changing paid-bounty behavior."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number:
        return "could not identify repository/issue number"

    reason = bounty.has_existing_implementation_pr(repo, number, token)
    if reason:
        return reason

    comments = issue_comments(item, token) if comments is None else comments
    reason = linked_open_pr_reason(item, token, comments)
    if reason:
        return reason

    return strategic_claim_reason(item, comments)


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

    with ThreadPoolExecutor(
        max_workers=min(NETWORK_WORKERS, max(1, len(detail_paths)))
    ) as executor:
        details = executor.map(
            lambda path: fetch_text("https://app.opire.dev" + path),
            detail_paths,
        )
        for detail in details:
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

    with ThreadPoolExecutor(
        max_workers=min(NETWORK_WORKERS, max(1, len(detail_paths)))
    ) as executor:
        details = executor.map(
            lambda path: fetch_text("https://www.bountyhub.dev" + path),
            detail_paths,
        )
        for detail in details:
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
    sources = (
        issuehunt_platform_refs,
        opire_platform_refs,
        bountyhub_platform_refs,
    )
    with ThreadPoolExecutor(max_workers=len(sources)) as executor:
        for source in executor.map(lambda loader: loader(), sources):
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
    activity_comments: list[dict[str, Any]] | None = None,
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

    maintainer_ready = maintainer_ready_signal(labels_text)
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

    if lane == "strategic":
        now = datetime.now(timezone.utc)
        created = bounty.parse_github_datetime(item.get("created_at"))
        updated = bounty.parse_github_datetime(item.get("updated_at"))
        created_days = max(0, (now - created).days) if created else None
        updated_days = max(0, (now - updated).days) if updated else None

        if updated_days is not None:
            if updated_days <= 14:
                career += 8
                career_reasons.append("issue active in last 14d")
            elif updated_days <= 60:
                career += 5
                career_reasons.append("issue active in last 60d")
            elif updated_days <= 180:
                career += 2
                career_reasons.append("issue active in last 180d")

        recent_comment_days: int | None = None
        recent_maintainer_days: int | None = None
        for comment in activity_comments or []:
            stamp = bounty.parse_github_datetime(
                comment.get("updated_at") or comment.get("created_at")
            )
            if not stamp:
                continue
            days = max(0, (now - stamp).days)
            if recent_comment_days is None or days < recent_comment_days:
                recent_comment_days = days
            association = str(comment.get("author_association", "")).upper()
            if association in TRUSTED_ASSOCIATIONS and (
                recent_maintainer_days is None or days < recent_maintainer_days
            ):
                recent_maintainer_days = days

        if recent_maintainer_days is not None and recent_maintainer_days <= 90:
            career += 8
            career_reasons.append("recent maintainer activity")
        elif recent_comment_days is not None and recent_comment_days <= 30:
            career += 4
            career_reasons.append("recent active discussion")

        recently_active = bool(
            (updated_days is not None and updated_days <= 180)
            or (recent_comment_days is not None and recent_comment_days <= 90)
            or (recent_maintainer_days is not None and recent_maintainer_days <= 180)
        )
        if created_days is not None and not maintainer_ready and not recently_active:
            if created_days > 730:
                career -= 15
                career_reasons.append("stale inactive backlog penalty")
            elif created_days > 365:
                career -= 8
                career_reasons.append("older inactive backlog penalty")

    if lane == "strategic" and documentation_microfix(item):
        career = min(career, 45)
        career_reasons.insert(0, "documentation-only micro-fix cap")

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
    maintainer_ready = maintainer_ready_signal(labels)
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


def strategic_rejection(
    item: Mapping[str, Any],
    token: str | None,
    comments: list[dict[str, Any]] | None = None,
) -> str | None:
    if not strategic_basic_candidate(item):
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

    label_set = issue_label_set(item)
    labels_text = " ".join(label_set)
    comment_ready, comment_hold_reason = maintainer_readiness_comment_state(item, comments)

    abandoned_reason = abandoned_lifecycle_reason(item, comment_ready is True)
    if abandoned_reason:
        return abandoned_reason

    accepted = (
        bool(TRIAGE_ACCEPTED_LABELS & label_set)
        or maintainer_ready_signal(labels_text)
        or comment_ready is True
    )

    readiness_reason = readiness_pending_label_reason(item, accepted)
    if readiness_reason:
        return readiness_reason

    pending = bool(TRIAGE_PENDING_LABELS & label_set) or triage_pending_signal(labels_text)
    if pending and not accepted:
        return "awaiting maintainer triage"

    tracking_reason = automated_tracking_issue_reason(item)
    if tracking_reason:
        return tracking_reason

    release_reason = release_tracking_reason(item, comments)
    if release_reason:
        return release_reason

    if comment_hold_reason:
        return comment_hold_reason

    diagnostic_reason = non_actionable_diagnostic_reason(item)
    if diagnostic_reason:
        return diagnostic_reason

    return strategic_competition_reason(item, token, comments)


def verify(
    item: Mapping[str, Any],
    token: str | None,
    repo_cache: dict[str, dict[str, Any]],
    guide_cache: dict[str, str | None],
    require_paid: bool = False,
    payment_signal_override: str | None = None,
    activity_comments: list[dict[str, Any]] | None = None,
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

    clean = bounty.is_clean_candidate(fresh) if require_paid else strategic_basic_candidate(fresh)
    if not clean:
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
        comments = issue_comments(fresh, token) if activity_comments is None else activity_comments
        reason = strategic_rejection(fresh, token, comments)
        if reason:
            return None, reason
        lane = "strategic"

    repo, _ = bounty.issue_repo_and_number(fresh)
    if repo is None:
        return None, "could not identify repository/issue number"
    with cache_lock_for(f"repo:{repo}"):
        if repo not in repo_cache:
            repo_cache[repo] = bounty.fetch_repo_metadata(repo, token)
    repo_meta = repo_cache[repo]
    if not repo_meta:
        return None, "repository metadata unavailable"
    if repo_meta.get("archived"):
        return None, "repository is archived"
    with cache_lock_for(f"guide:{repo}"):
        if repo not in guide_cache:
            guide_cache[repo] = contribution_guide(repo, token)
    return (
        build_candidate(
            fresh,
            lane,
            signal,
            repo_meta,
            guide_cache[repo],
            comments if lane == "strategic" else None,
        ),
        None,
    )


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
    pending: list[tuple[dict[str, Any], str | None, bool]] = []

    for query in PAID_DISCOVERY_QUERIES:
        for item in bounty.search_github(query, token).get("items", []):
            url = item.get("html_url")
            if not url or url in seen or url in touched:
                continue
            touched.add(url)
            if bounty.is_clean_candidate(item):
                pending.append((item, None, False))

    # Official platform feeds can expose funded issues that contain no bounty
    # keywords on GitHub at all. Fetch their source issues concurrently, then
    # apply the same source-authoritative verification as direct discoveries.
    platform_sources: list[tuple[str, str]] = []
    for source_url, platform_signal in platform_paid_refs().items():
        if source_url in seen or source_url in touched:
            continue
        touched.add(source_url)
        platform_sources.append((source_url, platform_signal))

    with ThreadPoolExecutor(
        max_workers=min(NETWORK_WORKERS, max(1, len(platform_sources)))
    ) as executor:
        platform_items = list(
            executor.map(
                lambda row: issue_from_github_url(row[0], token),
                platform_sources,
            )
        )

    for (source_url, platform_signal), item in zip(platform_sources, platform_items, strict=True):
        if item and bounty.is_clean_candidate(item):
            pending.append((item, platform_signal, True))

    def verify_paid(
        row: tuple[dict[str, Any], str | None, bool],
    ) -> tuple[
        tuple[dict[str, Any], str | None, bool],
        tuple[dict[str, Any] | None, str | None],
    ]:
        item, platform_signal, _ = row
        return (
            row,
            verify(
                item,
                token,
                repo_cache,
                guide_cache,
                require_paid=True,
                payment_signal_override=platform_signal,
            ),
        )

    with ThreadPoolExecutor(max_workers=min(NETWORK_WORKERS, max(1, len(pending)))) as executor:
        verification_results = list(executor.map(verify_paid, pending))

    for (item, _, is_platform), (candidate, reason) in verification_results:
        url = str(item.get("html_url") or "")
        kind = "platform" if is_platform else "paid"
        if reason:
            add_reject(rejected, examples, item, reason)
            print(f"Skipping {kind} candidate {url}: {reason}")
            continue

        assert candidate is not None
        if candidate["cash_score"] < PAID_MIN_CASH_SCORE:
            reason = (
                f"cash score {candidate['cash_score']}/100 below paid threshold "
                f"{PAID_MIN_CASH_SCORE}/100"
            )
            add_reject(rejected, examples, item, reason)
            print(f"Skipping {kind} candidate {url}: {reason}")
            continue

        found.append(candidate)

    return found, rejected, examples


def possible_miss_signal(item: Mapping[str, Any]) -> bool:
    """Flag strong raw results that deserve scrutiny when filters discard them."""
    _, _, labels, text = issue_text(item)
    updated = bounty.parse_github_datetime(item.get("updated_at"))
    recent = bool(updated and (datetime.now(timezone.utc) - updated).days <= 60)
    contributor_signal = any(
        marker in labels
        for marker in (
            "help wanted",
            "help-wanted",
            "good first issue",
            "triage/accepted",
            "refined",
        )
    )
    bug_signal = "bug" in labels or bool(
        re.search(r"\b(?:bug|regression|panic|deadlock|leak)\b", text)
    )
    return recent and (contributor_signal or bug_signal)


def basic_rejection_audit_reason(item: Mapping[str, Any]) -> str | None:
    """Return only tunable/unknown basic-filter reasons worth auditing."""
    if "pull_request" in item:
        return None
    if item.get("assignees"):
        return None

    url = str(item.get("html_url", "")).lower()
    title = str(item.get("title", "")).lower()
    body = str(item.get("body", "")).lower()
    if "/bountyscout/issues/" in url:
        return None
    if any(
        marker in title or marker in body
        for marker in (
            "bounty alert:",
            "active bounty scan results",
            "new opportunities found",
            "new opportunityies found",
        )
    ):
        return None
    if any(
        term in title or term in body
        for term in (
            "airdrop",
            "referral",
            "casino",
            "gambling",
            "trading bot",
            "blog post",
            "article writing",
            "tutorial proposal",
            "content creator",
        )
    ):
        return None
    return "strong-looking result rejected by an unrecognized basic eligibility filter rule"


def add_audit(
    audit: list[dict[str, Any]],
    item: Mapping[str, Any],
    reason: str,
) -> None:
    if len(audit) >= STRATEGIC_AUDIT_LIMIT:
        return
    audit.append(
        {
            "url": item.get("html_url"),
            "title": item.get("title"),
            "reason": reason,
        }
    )


def strategic_inspection_items(
    provisional: list[tuple[int, int, int, dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Keep the best configured result pool per repo for activity inspection."""
    by_repo: dict[str, list[tuple[int, int, int, dict[str, Any]]]] = {}
    for row in provisional:
        repo, _ = bounty.issue_repo_and_number(row[3])
        if repo:
            by_repo.setdefault(repo, []).append(row)

    inspected: dict[str, list[dict[str, Any]]] = {}
    for repo, rows in by_repo.items():
        rows.sort(key=lambda row: row[:3], reverse=True)
        inspected[repo] = [row[3] for row in rows[:STRATEGIC_INSPECT_PER_REPO]]
    return inspected


def discover_strategic(
    token: str | None,
    seen: set[str],
    paid_urls: set[str],
    repo_cache: dict[str, dict[str, Any]],
    guide_cache: dict[str, str | None],
) -> tuple[
    list[dict[str, Any]],
    dict[str, int],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    provisional: list[tuple[int, int, int, dict[str, Any]]] = []
    touched: set[str] = set()
    rejected: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []

    source_batches: list[list[dict[str, Any]]] = []
    with ThreadPoolExecutor(
        max_workers=min(NETWORK_WORKERS, max(1, len(TARGET_REPOS)))
    ) as executor:
        repo_results = executor.map(
            lambda target_repo: (
                target_repo,
                target_repo_issue_pool(target_repo, token),
            ),
            TARGET_REPOS,
        )
        for target_repo, (items, source_error) in repo_results:
            if source_error:
                add_audit(
                    audit,
                    {
                        "html_url": f"https://github.com/{target_repo}/issues",
                        "title": target_repo,
                    },
                    source_error,
                )
            source_batches.append(items)

    for query in STRATEGIC_GLOBAL_QUERIES:
        source_batches.append(
            bounty.search_github(query, token, per_page=STRATEGIC_SEARCH_PER_PAGE).get("items", [])
        )

    for items in source_batches:
        for item in items:
            url = item.get("html_url")
            if not url or url in seen or url in paid_urls or url in touched:
                continue
            touched.add(url)
            if not strategic_basic_candidate(item):
                audit_reason = (
                    basic_rejection_audit_reason(item) if possible_miss_signal(item) else None
                )
                if audit_reason:
                    add_audit(audit, item, audit_reason)
                continue
            repo, _ = bounty.issue_repo_and_number(item)
            if not repo:
                continue
            with cache_lock_for(f"repo:{repo}"):
                if repo not in repo_cache:
                    repo_cache[repo] = bounty.fetch_repo_metadata(repo, token)
            meta = repo_cache[repo]
            if not meta or meta.get("archived"):
                if possible_miss_signal(item):
                    add_audit(
                        audit,
                        item,
                        "strong-looking result skipped because repository metadata is unavailable or archived",
                    )
                continue
            signal = bounty.payment_signal(item)
            lane = "paid" if signal else "strategic"
            preview = build_candidate(item, lane, signal, meta, None)
            provisional.append(
                (
                    preview["priority_score"],
                    preview["career_score"],
                    preview["cash_score"],
                    item,
                )
            )

    inspected = strategic_inspection_items(provisional)
    inspected_urls = {
        str(item.get("html_url"))
        for items in inspected.values()
        for item in items
        if item.get("html_url")
    }
    for _, _, _, item in provisional:
        url = str(item.get("html_url") or "")
        if url and url not in inspected_urls and possible_miss_signal(item):
            add_audit(
                audit,
                item,
                "strong-looking result fell outside the repo top-15 inspection pool",
            )

    inspection_rows = [(repo, item) for repo, items in inspected.items() for item in items]
    with ThreadPoolExecutor(
        max_workers=min(NETWORK_WORKERS, max(1, len(inspection_rows)))
    ) as executor:
        comment_batches = list(
            executor.map(
                lambda row: issue_comments(row[1], token),
                inspection_rows,
            )
        )

    ranked_by_repo: dict[
        str,
        list[tuple[int, int, int, dict[str, Any], list[dict[str, Any]]]],
    ] = {}
    for (repo, item), comments in zip(inspection_rows, comment_batches, strict=True):
        signal = bounty.payment_signal(item)
        lane = "paid" if signal else "strategic"
        preview = build_candidate(
            item,
            lane,
            signal,
            repo_cache[repo],
            None,
            comments,
        )
        ranked_by_repo.setdefault(repo, []).append(
            (
                preview["priority_score"],
                preview["career_score"],
                preview["cash_score"],
                item,
                comments,
            )
        )

    verification_rows: list[
        tuple[str, tuple[int, int, int, dict[str, Any], list[dict[str, Any]]]]
    ] = []
    for repo in inspected:
        ranked = ranked_by_repo.get(repo, [])
        ranked.sort(key=lambda row: row[:3], reverse=True)
        verification_rows.extend((repo, row) for row in ranked)

    def verify_row(
        entry: tuple[
            str,
            tuple[int, int, int, dict[str, Any], list[dict[str, Any]]],
        ],
    ) -> tuple[
        tuple[
            str,
            tuple[int, int, int, dict[str, Any], list[dict[str, Any]]],
        ],
        tuple[dict[str, Any] | None, str | None],
    ]:
        repo, row = entry
        item = row[3]
        comments = row[4]
        return (
            (repo, row),
            verify(
                item,
                token,
                repo_cache,
                guide_cache,
                activity_comments=comments,
            ),
        )

    with ThreadPoolExecutor(
        max_workers=min(NETWORK_WORKERS, max(1, len(verification_rows)))
    ) as executor:
        verification_results = list(executor.map(verify_row, verification_rows))

    verified_by_repo: dict[str, list[dict[str, Any]]] = {repo: [] for repo in inspected}
    for (repo, row), (candidate, reason) in verification_results:
        item = row[3]
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
            if possible_miss_signal(item):
                add_audit(
                    audit,
                    item,
                    f"strong-looking near miss: {reason}",
                )
            print(f"Skipping strategic candidate {item.get('html_url')}: {reason}")
            continue

        verified_by_repo[repo].append(candidate)

    found: list[dict[str, Any]] = []
    for repo in inspected:
        verified_repo = verified_by_repo[repo]
        verified_repo.sort(
            key=lambda item: (
                item["priority_score"],
                item["career_score"],
                item["cash_score"],
                -item["comments"],
            ),
            reverse=True,
        )
        found.extend(verified_repo[:STRATEGIC_KEEP_PER_REPO])

    return found, rejected, examples, audit


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

    started = monotonic()
    paid_started = monotonic()
    paid, paid_rejects, paid_examples = discover_paid(token, seen, repo_cache, guide_cache)
    paid_seconds = monotonic() - paid_started

    strategic_started = monotonic()
    strategic, strategic_rejects, strategic_examples, strategic_audit = discover_strategic(
        token, seen, {x["url"] for x in paid}, repo_cache, guide_cache
    )
    strategic_seconds = monotonic() - strategic_started
    print(
        "Scout performance: "
        f"paid={paid_seconds:.1f}s, strategic={strategic_seconds:.1f}s, "
        f"total={monotonic() - started:.1f}s"
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
    if strategic_audit:
        print("=== POTENTIAL SCANNER MISSES ===")
        for item in strategic_audit:
            print(f"- {item['url']}: {item['reason']}")

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
        if strategic_audit:
            body += "\n### Potential scanner misses / tuning candidates\n\n"
            for item in strategic_audit[:12]:
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
