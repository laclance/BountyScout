from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Any, Mapping, cast

from bountyscout import github
import scout_bounties as bounty
from bountyscout import reporting
from bountyscout import scoring
from bountyscout import sources
from bountyscout.strategic import competition as competition_policy
from bountyscout.strategic.claims import strategic_claim_text as strategic_claim_text
from bountyscout.strategic.readiness import (
    TRUSTED_ASSOCIATIONS as TRUSTED_ASSOCIATIONS,
    abandoned_lifecycle_reason as abandoned_lifecycle_reason,
    automated_tracking_issue_reason as automated_tracking_issue_reason,
    issue_label_set as issue_label_set,
    maintainer_comment_authority as maintainer_comment_authority,
    maintainer_issue_decision_reason as maintainer_issue_decision_reason,
    maintainer_submission_hold_reason as maintainer_submission_hold_reason,
    manual_tracking_issue_reason as manual_tracking_issue_reason,
    maintainer_readiness_comment_state as maintainer_readiness_comment_state,
    proposal_stage_signal as proposal_stage_signal,
    reporter_resolution_reason as reporter_resolution_reason,
    reporter_support_triage_reason as reporter_support_triage_reason,
    reward_history_reason as reward_history_reason,
    security_disclosure_reason as security_disclosure_reason,
    readiness_pending_label_reason as readiness_pending_label_reason,
    release_tracking_reason as release_tracking_reason,
    triage_pending_signal as triage_pending_signal,
)
from bountyscout.types import (
    Candidate,
    CandidateLane,
    GitHubComment,
    GitHubIssue,
    RejectionRecord,
    RepositoryMetadata,
)

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
    'is:issue is:open no:assignee label:"help wanted" (label:"bug" OR regression) sort:updated-desc',
    'is:issue is:open no:assignee label:"good first issue" label:"bug" sort:updated-desc',
]
STRATEGIC_SEARCH_PER_PAGE = 20
STRATEGIC_GLOBAL_SEARCH_PER_PAGE = 30
TARGET_REPO_FETCH_PER_PAGE = 50
TARGET_REPO_FETCH_PAGES = 3
STRATEGIC_INSPECT_PER_REPO = 15
STRATEGIC_ADAPTIVE_INSPECT_BUDGET = 24
STRATEGIC_KEEP_PER_REPO = 3
STRATEGIC_VERIFY_SCORE_UPLIFT_BOUND = 11
STRATEGIC_REFRESH_FAILURE_LIMIT = 2
STRATEGIC_COVERAGE_WARNING_THRESHOLD = 5
STRATEGIC_MIN_CAREER_SCORE = 55
STRATEGIC_AUDIT_LIMIT = 20
PAID_MIN_CASH_SCORE = 55
REPORT_LIMIT = 8
NETWORK_WORKERS = 6
STRATEGIC_VERIFY_WORKERS = 8
DISCOVERY_SEARCH_INTERVAL_SECONDS = 2.1
CACHE_LOCKS = github.KeyedLockPool()


PAID_DISCOVERY_QUERIES = [
    "is:issue is:open bounty in:title,body sort:updated-desc",
    'is:issue is:open "/reward" in:comments sort:updated-desc',
    "is:issue is:open (opire.dev OR bountyhub.dev OR algora.io) in:comments sort:updated-desc",
]
EXTENDED_AMOUNT_RE = (
    r"(?:[$€£¥₹]\s*\d[\d,]*(?:\.\d+)?|"
    r"(?<![A-Za-z])R\s*\d[\d,]*(?:\.\d+)?|"
    r"(?<!ERC-)(?<!\d)\d+(?:\.\d+)?\s*(?:usd|usdc|usdt|eur|gbp|cad|aud|nzd|jpy|chf|"
    r"inr|zar|dai|xmr|sol|eth|btc)\b)"
)

PLATFORM_FETCH_LIMIT = 20
ISSUEHUNT_PAGES = 2


def target_repo_issue_pool(
    repo: str,
    token: str | None,
) -> tuple[list[GitHubIssue], str | None]:
    """Fetch the configured bounded source pool for a curated repository."""
    return sources.target_repo_issue_pool(
        repo,
        token,
        fetch_per_page=TARGET_REPO_FETCH_PER_PAGE,
        fetch_pages=TARGET_REPO_FETCH_PAGES,
        result_limit=STRATEGIC_SEARCH_PER_PAGE,
    )


def maintainer_ready_signal(labels_text: str) -> bool:
    """Compatibility wrapper for contributor-ready label detection."""
    return scoring.maintainer_ready_signal(labels_text)


def issue_text(item: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Compatibility wrapper for normalized issue text."""
    return scoring.issue_text(item)


def code_reference_count(text: str) -> int:
    """Compatibility wrapper for source/config reference counting."""
    return scoring.code_reference_count(text)


def strategic_basic_candidate(item: GitHubIssue) -> bool:
    """Apply strategic eligibility without making comment volume disqualifying."""
    if bounty.is_clean_candidate(item):
        return True
    if int(item.get("comments") or 0) <= bounty.MAX_COMMENTS:
        return False

    relaxed = dict(item)
    relaxed["comments"] = bounty.MAX_COMMENTS
    return bounty.is_clean_candidate(relaxed)


def github_get_optional(url: str, token: str | None) -> Any:
    """Compatibility wrapper for optional GitHub JSON fetching."""
    return github.github_get(url, token, timeout=10, log_errors=False)


def fetch_text(url: str, timeout: int = 12) -> str:
    """Compatibility wrapper for public platform HTML fetching."""
    return sources.fetch_text(url, timeout)


def issue_comments(item: GitHubIssue, token: str | None) -> list[GitHubComment]:
    """Compatibility wrapper for issue-comment GitHub fetching."""
    return github.issue_comments(item, token)


TRIAGE_PENDING_LABELS = {"needs-triage"}
TRIAGE_ACCEPTED_LABELS = {"triage/accepted", "good first issue", "help wanted"}
STRATEGIC_CLAIM_MAX_AGE_DAYS = competition_policy.STRATEGIC_CLAIM_MAX_AGE_DAYS


def strategic_claim_reason(
    item: GitHubIssue,
    comments: list[GitHubComment],
) -> str | None:
    """Compatibility wrapper for strategic active-claim detection."""
    return competition_policy.strategic_claim_reason(item, comments)


def linked_open_pr_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Compatibility wrapper for explicitly linked implementation PR detection."""
    loaded_comments = issue_comments(item, token) if comments is None else comments
    return competition_policy.linked_open_pr_reason(item, token, loaded_comments)


def timeline_open_pr_reason(item: GitHubIssue, token: str | None) -> str | None:
    """Compatibility wrapper for timeline-linked implementation PR detection."""
    return competition_policy.timeline_open_pr_reason(item, token)


def supplemental_claim_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Compatibility wrapper for supplemental active-claim detection."""
    loaded_comments = issue_comments(item, token) if comments is None else comments
    return competition_policy.supplemental_claim_reason(item, loaded_comments)


def extended_competition_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Apply paid-compatible competition checks through the extracted policy module."""
    loaded_comments = issue_comments(item, token) if comments is None else comments
    return competition_policy.extended_competition_reason(
        item,
        token,
        loaded_comments,
        linked_pr_checker=linked_open_pr_reason,
        supplemental_claim_checker=lambda candidate, claim_comments: supplemental_claim_reason(
            candidate,
            token,
            claim_comments,
        ),
    )


def strategic_competition_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Apply strategic-only competition checks through the extracted policy module."""
    loaded_comments = issue_comments(item, token) if comments is None else comments
    return competition_policy.strategic_competition_reason(
        item,
        token,
        loaded_comments,
        timeline_pr_checker=timeline_open_pr_reason,
        linked_pr_checker=linked_open_pr_reason,
        strategic_claim_checker=strategic_claim_reason,
    )


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


def comment_payment_signal(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Recognize payment signals while reusing comments already loaded by verification."""
    loaded_comments = issue_comments(item, token) if comments is None else comments

    # Prefer explicit bot/platform confirmations over the command that triggered them.
    for comment in loaded_comments:
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
    for comment in loaded_comments:
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


def issue_from_github_url(url: str, token: str | None) -> GitHubIssue | None:
    """Compatibility wrapper for platform-discovered GitHub issue fetching."""
    return github.issue_from_github_url(url, token)


def issuehunt_platform_refs() -> dict[str, str]:
    """Compatibility wrapper for IssueHunt discovery."""
    return sources.issuehunt_platform_refs(fetch_text, pages=ISSUEHUNT_PAGES)


def opire_platform_refs() -> dict[str, str]:
    """Compatibility wrapper for Opire discovery."""
    return sources.opire_platform_refs(
        fetch_text,
        fetch_limit=PLATFORM_FETCH_LIMIT,
        network_workers=NETWORK_WORKERS,
    )


def bountyhub_platform_refs() -> dict[str, str]:
    """Compatibility wrapper for BountyHub discovery."""
    return sources.bountyhub_platform_refs(
        EXTENDED_AMOUNT_RE,
        fetch_text,
        fetch_limit=PLATFORM_FETCH_LIMIT,
        network_workers=NETWORK_WORKERS,
    )


def platform_paid_refs() -> dict[str, str]:
    """Merge official bounty-platform source discoveries."""
    return sources.platform_paid_refs(
        (
            issuehunt_platform_refs,
            opire_platform_refs,
            bountyhub_platform_refs,
        ),
        network_workers=NETWORK_WORKERS,
    )


def contribution_guide(repo: str, token: str | None) -> str | None:
    """Compatibility wrapper for contribution-guide GitHub fetching."""
    return github.contribution_guide(repo, token, github_get_optional)


def fetch_repo_metadata(repo: str, token: str | None) -> RepositoryMetadata:
    """Compatibility wrapper for repository metadata GitHub fetching."""
    return github.repo_metadata(repo, token)


def build_candidate(
    item: Mapping[str, Any],
    lane: CandidateLane,
    signal: str | None,
    repo_meta: Mapping[str, Any],
    guide: str | None,
    activity_comments: list[GitHubComment] | None = None,
) -> Candidate:
    """Build ranked candidate output through the extracted scoring module."""
    return scoring.build_candidate(
        item,
        lane,
        signal,
        repo_meta,
        guide,
        activity_comments,
        target_repos=TARGET_REPOS,
        amount_pattern=EXTENDED_AMOUNT_RE,
    )


def refresh_issue(
    item: GitHubIssue, token: str | None
) -> tuple[GitHubIssue | None, str | None]:
    repo, number = github.issue_repo_and_number(item)
    if not repo or not number:
        return None, "could not identify repository/issue number"
    fresh = github.github_get(f"https://api.github.com/repos/{repo}/issues/{number}", token)
    if not isinstance(fresh, dict):
        return None, "could not refresh source issue"
    if fresh.get("state") != "open":
        return None, "issue is no longer open"
    if "pull_request" in fresh:
        return None, "source is a pull request, not an issue"
    return cast(GitHubIssue, fresh), None


def upstream_wrapper_issue_url(item: GitHubIssue) -> str | None:
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


def non_actionable_diagnostic_reason(item: GitHubIssue) -> str | None:
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


def _strategic_common_source_rejection(item: GitHubIssue) -> str | None:
    """Return source-only strategic rejections shared by preflight and full verification."""
    if not strategic_basic_candidate(item):
        return "failed basic eligibility filter"

    _, _, labels, text = issue_text(item)
    if "oss opportunity queue" in text:
        return "generated opportunity-scout report"
    if any(
        label in labels
        for label in (
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

    if "claimed" in issue_label_set(item):
        return "issue is marked claimed by the project"
    return None


def _strategic_classification_rejection(
    item: GitHubIssue,
    comments: list[GitHubComment],
) -> str | None:
    """Return ordered policy rejections that are pure over supplied issue evidence."""
    for reason in (
        security_disclosure_reason(item),
        reporter_support_triage_reason(item),
        manual_tracking_issue_reason(item, comments),
        automated_tracking_issue_reason(item),
        release_tracking_reason(item, comments),
        maintainer_issue_decision_reason(item),
        maintainer_submission_hold_reason(item),
    ):
        if reason:
            return reason
    return None


def strategic_preflight_rejection(item: GitHubIssue) -> str | None:
    """Reject source-visible states that cannot be rescued by comment/timeline checks."""
    common_reason = _strategic_common_source_rejection(item)
    if common_reason:
        return common_reason

    label_set = issue_label_set(item)
    if not int(item.get("comments") or 0):
        labels_text = " ".join(label_set)
        accepted = bool(TRIAGE_ACCEPTED_LABELS & label_set) or maintainer_ready_signal(labels_text)
        abandoned_reason = abandoned_lifecycle_reason(item, False)
        if abandoned_reason:
            return abandoned_reason
        readiness_reason = readiness_pending_label_reason(item, accepted)
        if readiness_reason:
            return readiness_reason
        pending = bool(TRIAGE_PENDING_LABELS & label_set) or triage_pending_signal(labels_text)
        if pending and not accepted:
            return "awaiting maintainer triage"

    classification_reason = _strategic_classification_rejection(item, [])
    if classification_reason:
        return classification_reason

    diagnostic_reason = non_actionable_diagnostic_reason(item)
    if diagnostic_reason:
        return diagnostic_reason

    return strategic_claim_reason(item, [])


def strategic_rejection(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    common_reason = _strategic_common_source_rejection(item)
    if common_reason:
        return common_reason

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

    classification_reason = _strategic_classification_rejection(item, comments or [])
    if classification_reason:
        return classification_reason

    reporter_reason = reporter_resolution_reason(item, comments)
    if reporter_reason:
        return reporter_reason

    if comment_hold_reason:
        return comment_hold_reason

    diagnostic_reason = non_actionable_diagnostic_reason(item)
    if diagnostic_reason:
        return diagnostic_reason

    return strategic_competition_reason(item, token, comments)


def verify(
    item: GitHubIssue,
    token: str | None,
    repo_cache: dict[str, RepositoryMetadata],
    guide_cache: dict[str, str | None],
    require_paid: bool = False,
    payment_signal_override: str | None = None,
    activity_comments: list[GitHubComment] | None = None,
) -> tuple[Candidate | None, str | None]:
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

    reward_history = reward_history_reason(fresh)
    if reward_history:
        return None, reward_history

    comments = activity_comments
    issue_signal = bounty.payment_signal(fresh) or supplemental_payment_signal(fresh)
    comment_signal = None
    if not issue_signal and int(fresh.get("comments") or 0):
        if require_paid:
            comment_signal = comment_payment_signal(fresh, token)
        else:
            if comments is None:
                comments, comments_reason = github.issue_comments_checked(fresh, token)
                if comments_reason:
                    return None, comments_reason
            comment_signal = comment_payment_signal(fresh, token, comments)
    signal = issue_signal or comment_signal or payment_signal_override

    lane: CandidateLane
    if require_paid or signal:
        issue_author_claim = strategic_claim_reason(fresh, [])
        if issue_author_claim:
            return None, issue_author_claim

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
            repo, number = github.issue_repo_and_number(fresh)
            competition_reason = extended_competition_reason(fresh, token, comments)
            if competition_reason:
                return None, competition_reason

        lane = "paid"
    else:
        if comments is None:
            comments, comments_reason = github.issue_comments_checked(fresh, token)
            if comments_reason:
                return None, comments_reason
        reason = strategic_rejection(fresh, token, comments)
        if reason:
            return None, reason
        lane = "strategic"

    repo, _ = github.issue_repo_and_number(fresh)
    if repo is None:
        return None, "could not identify repository/issue number"
    repo_meta = github.cached_value(
        repo_cache,
        repo,
        lambda: fetch_repo_metadata(repo, token),
        CACHE_LOCKS,
        namespace="repo",
    )
    if not repo_meta:
        return None, "repository metadata unavailable"
    if repo_meta.get("archived"):
        return None, "repository is archived"
    guide = github.cached_value(
        guide_cache,
        repo,
        lambda: contribution_guide(repo, token),
        CACHE_LOCKS,
        namespace="guide",
    )
    return (
        build_candidate(
            fresh,
            lane,
            signal,
            repo_meta,
            guide,
            comments if lane == "strategic" else None,
        ),
        None,
    )


def add_reject(
    counts: dict[str, int], examples: list[RejectionRecord], item: Mapping[str, Any], reason: str
) -> None:
    counts[reason] = counts.get(reason, 0) + 1
    if len(examples) < 12:
        examples.append({"url": item.get("html_url"), "title": item.get("title"), "reason": reason})


def discover_paid(
    token: str | None,
    seen: set[str],
    repo_cache: dict[str, RepositoryMetadata],
    guide_cache: dict[str, str | None],
    search_results: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[list[Candidate], dict[str, int], list[RejectionRecord]]:
    found: list[Candidate] = []
    touched: set[str] = set()
    rejected: dict[str, int] = {}
    examples: list[RejectionRecord] = []
    pending: list[tuple[GitHubIssue, str | None, bool]] = []

    if search_results is None:
        search_results = [
            (query, bounty.search_github(query, token)) for query in PAID_DISCOVERY_QUERIES
        ]

    for _, result in search_results:
        items = result.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
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
        row: tuple[GitHubIssue, str | None, bool],
    ) -> tuple[
        tuple[GitHubIssue, str | None, bool],
        tuple[Candidate | None, str | None],
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


def possible_miss_signal(item: GitHubIssue) -> bool:
    """Flag strong raw results that deserve scrutiny when filters discard them."""
    if (
        security_disclosure_reason(item)
        or reward_history_reason(item)
        or manual_tracking_issue_reason(item)
        or automated_tracking_issue_reason(item)
        or release_tracking_reason(item)
    ):
        return False

    _, _, labels, text = issue_text(item)
    updated = github.parse_github_datetime(item.get("updated_at"))
    recent = bool(updated and (datetime.now(timezone.utc) - updated).days <= 60)
    contributor_signal = any(
        marker in labels
        for marker in (
            "help wanted",
            "help-wanted",
            "good first issue",
            "triage/accepted",
            "refined",
            "contributor/wanted",
            "contributor wanted",
        )
    )
    bug_signal = "bug" in labels or bool(
        re.search(r"\b(?:bug|regression|panic|deadlock|leak)\b", text)
    )
    return recent and (contributor_signal or bug_signal)


def basic_rejection_audit_reason(item: GitHubIssue) -> str | None:
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
    audit: list[RejectionRecord],
    item: GitHubIssue,
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
    provisional: list[sources.IssueRow],
) -> dict[str, list[GitHubIssue]]:
    """Select base per-repo candidates plus a globally bounded strong overflow."""
    return sources.strategic_inspection_items(
        provisional,
        base_per_repo=STRATEGIC_INSPECT_PER_REPO,
        adaptive_budget=STRATEGIC_ADAPTIVE_INSPECT_BUDGET,
        should_expand=possible_miss_signal,
    )


def strategic_global_search_results(
    token: str | None,
) -> list[tuple[str, dict[str, Any]]]:
    """Reserve the strategic global Search calls before heavier API work begins."""
    return [
        (
            query,
            bounty.search_github(query, token, per_page=STRATEGIC_GLOBAL_SEARCH_PER_PAGE),
        )
        for query in STRATEGIC_GLOBAL_QUERIES
    ]


def prefetch_discovery_searches(
    token: str | None,
) -> tuple[
    list[tuple[str, dict[str, Any]]],
    list[tuple[str, dict[str, Any]]],
]:
    """Pace paid and strategic Search calls to avoid burst/secondary rate limits."""
    requests = [("paid", query, 15) for query in PAID_DISCOVERY_QUERIES] + [
        ("strategic", query, STRATEGIC_GLOBAL_SEARCH_PER_PAGE) for query in STRATEGIC_GLOBAL_QUERIES
    ]
    paid_results: list[tuple[str, dict[str, Any]]] = []
    strategic_results: list[tuple[str, dict[str, Any]]] = []
    for index, (lane, query, per_page) in enumerate(requests):
        result = bounty.search_github(query, token, per_page=per_page)
        target = paid_results if lane == "paid" else strategic_results
        target.append((query, result))
        if index + 1 < len(requests):
            sleep(DISCOVERY_SEARCH_INTERVAL_SECONDS)
    return paid_results, strategic_results


def discover_strategic(
    token: str | None,
    seen: set[str],
    paid_urls: set[str],
    repo_cache: dict[str, RepositoryMetadata],
    guide_cache: dict[str, str | None],
    global_search_results: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[
    list[Candidate],
    dict[str, int],
    list[RejectionRecord],
    list[RejectionRecord],
]:
    provisional: list[sources.IssueRow] = []
    touched: set[str] = set()
    rejected: dict[str, int] = {}
    examples: list[RejectionRecord] = []
    audit: list[RejectionRecord] = []

    source_batches: list[list[GitHubIssue]] = []
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

    if global_search_results is None:
        global_search_results = strategic_global_search_results(token)

    for query, result in global_search_results:
        global_items = result.get("items")
        if not isinstance(global_items, list):
            add_audit(
                audit,
                {
                    "html_url": "https://github.com/issues",
                    "title": f"Global GitHub Search: {query}",
                },
                f"global strategic discovery search failed for query: {query}; "
                "scan coverage incomplete",
            )
            continue
        source_batches.append(global_items)

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
            repo, _ = github.issue_repo_and_number(item)
            if not repo:
                continue
            repo_key = repo
            meta = github.cached_value(
                repo_cache,
                repo_key,
                lambda: fetch_repo_metadata(repo_key, token),
                CACHE_LOCKS,
                namespace="repo",
            )
            if not meta or meta.get("archived"):
                if possible_miss_signal(item):
                    add_audit(
                        audit,
                        item,
                        "strong-looking result skipped because repository metadata is unavailable or archived",
                    )
                continue
            signal = bounty.payment_signal(item)
            lane: CandidateLane = "paid" if signal else "strategic"
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
                "strong-looking result fell outside the adaptive repo inspection pool",
            )

    ranked_by_repo: dict[str, list[sources.IssueRow]] = {}
    for repo, items in inspected.items():
        ranked: list[sources.IssueRow] = []
        for item in items:
            signal = bounty.payment_signal(item)
            lane = "paid" if signal else "strategic"
            preview = build_candidate(
                item,
                lane,
                signal,
                repo_cache[repo],
                None,
            )
            ranked.append(
                (
                    preview["priority_score"],
                    preview["career_score"],
                    preview["cash_score"],
                    item,
                )
            )
        ranked.sort(key=lambda row: row[:3], reverse=True)
        ranked_by_repo[repo] = ranked

    source_failure_reasons = {
        "could not refresh source issue",
        "could not refresh issue comments",
        "could not verify open implementation PR timeline",
    }

    def verify_repo(
        entry: tuple[str, list[sources.IssueRow]],
    ) -> tuple[
        str,
        list[
            tuple[
                sources.IssueRow,
                Candidate | None,
                str | None,
                bool,
            ]
        ],
        bool,
    ]:
        repo, ranked = entry
        outcomes: list[
            tuple[
                sources.IssueRow,
                Candidate | None,
                str | None,
                bool,
            ]
        ] = []
        accepted: list[Candidate] = []
        consecutive_source_failures = 0

        for index, row in enumerate(ranked):
            item = row[3]
            candidate: Candidate | None
            reason: str | None
            network_checked: bool
            preflight_reason = strategic_preflight_rejection(item)
            career_upper_bound = sources.strategic_verification_upper_bound(
                row,
                score_uplift_bound=STRATEGIC_VERIFY_SCORE_UPLIFT_BOUND,
            )[1]
            if preflight_reason:
                candidate = None
                reason = preflight_reason
                network_checked = False
            elif career_upper_bound < STRATEGIC_MIN_CAREER_SCORE:
                candidate = None
                reason = (
                    f"career score {row[1]}/100 below strategic threshold "
                    f"{STRATEGIC_MIN_CAREER_SCORE}/100"
                )
                network_checked = False
            else:
                candidate, reason = verify(
                    item,
                    token,
                    repo_cache,
                    guide_cache,
                )
                network_checked = True
            outcomes.append((row, candidate, reason, network_checked))

            if reason in source_failure_reasons:
                consecutive_source_failures += 1
            else:
                consecutive_source_failures = 0

            if (
                candidate is not None
                and reason is None
                and candidate["career_score"] >= STRATEGIC_MIN_CAREER_SCORE
            ):
                accepted.append(candidate)

            if consecutive_source_failures >= STRATEGIC_REFRESH_FAILURE_LIMIT:
                return repo, outcomes, True

            remaining = ranked[index + 1 :]
            if sources.strategic_repo_slots_settled(
                accepted,
                remaining,
                keep_per_repo=STRATEGIC_KEEP_PER_REPO,
                score_uplift_bound=STRATEGIC_VERIFY_SCORE_UPLIFT_BOUND,
            ):
                return repo, outcomes, False

        return repo, outcomes, False

    verification_inputs = list(ranked_by_repo.items())
    with ThreadPoolExecutor(
        max_workers=min(STRATEGIC_VERIFY_WORKERS, max(1, len(verification_inputs)))
    ) as executor:
        repo_verification_results = list(executor.map(verify_repo, verification_inputs))

    verified_by_repo: dict[str, list[Candidate]] = {repo: [] for repo in inspected}
    verified_rows = 0
    selected_rows = sum(len(rows) for rows in ranked_by_repo.values())
    for repo, outcomes, coverage_incomplete in repo_verification_results:
        verified_rows += sum(1 for _, _, _, network_checked in outcomes if network_checked)
        for row, candidate, reason, _ in outcomes:
            item = row[3]
            if reason:
                add_reject(rejected, examples, item, reason)
                if reason.startswith("career score ") and possible_miss_signal(item):
                    add_audit(audit, item, f"strong-looking near miss: {reason}")
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

        if coverage_incomplete:
            remaining = ranked_by_repo[repo][len(outcomes) :]
            if remaining:
                add_audit(
                    audit,
                    remaining[0][3],
                    f"verification coverage incomplete for {repo} after repeated source failures",
                )
            print(
                "Strategic verification coverage incomplete for "
                f"{repo}; stopped after {len(outcomes)} deep checks"
            )

    print(
        "Strategic deep verification: "
        f"{verified_rows}/{selected_rows} inspected rows required network checks"
    )

    found: list[Candidate] = []
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


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo_fullname = os.environ.get("GITHUB_REPOSITORY")
    seen = bounty.load_seen_bounties()
    repo_cache: dict[str, RepositoryMetadata] = {}
    guide_cache: dict[str, str | None] = {}

    started = monotonic()
    prefetched_paid_searches: list[tuple[str, dict[str, Any]]] | None = None
    prefetched_global_searches: list[tuple[str, dict[str, Any]]] | None = None
    if token and repo_fullname:
        prefetched_paid_searches, prefetched_global_searches = prefetch_discovery_searches(token)
    paid_started = monotonic()
    paid, paid_rejects, paid_examples = discover_paid(
        token, seen, repo_cache, guide_cache, prefetched_paid_searches
    )
    paid_seconds = monotonic() - paid_started

    strategic_started = monotonic()
    strategic, strategic_rejects, strategic_examples, strategic_audit = discover_strategic(
        token,
        seen,
        {x["url"] for x in paid},
        repo_cache,
        guide_cache,
        prefetched_global_searches,
    )
    if prefetched_paid_searches is not None:
        for query, result in prefetched_paid_searches:
            if not isinstance(result.get("items"), list):
                add_audit(
                    strategic_audit,
                    {
                        "html_url": "https://github.com/issues",
                        "title": f"Paid GitHub Search: {query}",
                    },
                    f"paid discovery search failed for query: {query}; scan coverage incomplete",
                )

    strategic_seconds = monotonic() - strategic_started
    print(
        "Scout performance: "
        f"paid={paid_seconds:.1f}s, strategic={strategic_seconds:.1f}s, "
        f"total={monotonic() - started:.1f}s"
    )

    by_url: dict[str, Candidate] = {}
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

    rejects = reporting.rejection_summary(paid_rejects, strategic_rejects)
    verification_failures = sum(
        strategic_rejects.get(reason, 0)
        for reason in (
            "could not refresh source issue",
            "could not refresh issue comments",
            "could not verify open implementation PR timeline",
        )
    )
    discovery_failures = sum(
        1
        for item in strategic_audit
        if "scan coverage incomplete" in str(item.get("reason", "")).lower()
    )
    coverage_failures = verification_failures + discovery_failures
    # A failed discovery batch can hide an entire source, so even one marks the run incomplete.
    coverage_warning = None
    if discovery_failures or verification_failures >= STRATEGIC_COVERAGE_WARNING_THRESHOLD:
        coverage_warning = (
            "Opportunity discovery/verification coverage is incomplete: "
            f"{coverage_failures} discovery/source/comment/competition checks failed, "
            "so this ranking may omit stronger candidates. "
            "Seen-state will not be advanced for this run."
        )
        print(f"WARNING: {coverage_warning}")

    if not queue and coverage_warning is None:
        print("No new verified OSS opportunities found.")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message = reporting.notification_message(queue, now, warning=coverage_warning)

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
        body = reporting.github_report_body(
            queue,
            now,
            verification_examples=paid_examples + strategic_examples,
            strategic_audit=strategic_audit,
            reject_counts=rejects,
            coverage_warning=coverage_warning,
        )
        delivered = (
            bounty.create_github_issue(
                repo_fullname,
                token,
                reporting.github_report_title(len(queue)),
                body,
            )
            or delivered
        )

    if rejects:
        print(
            "Filtered verified candidates: "
            + ", ".join(f"{k}={v}" for k, v in sorted(rejects.items()))
        )

    if attempted and delivered and coverage_warning is None:
        seen.update(x["url"] for x in queue)
        bounty.save_seen_bounties(seen)
    elif attempted and delivered:
        print("Verification coverage incomplete; state was not updated.")
    else:
        print("No notification was delivered; state was not updated.")


if __name__ == "__main__":
    main()
