"""Strategic competition and implementation-PR detection.

This module interprets claim and pull-request evidence for OSS opportunities. It
depends on the upstream GitHub helpers but never imports the application
orchestrator, so it can be tested directly with mocked GitHub responses.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import github_access as github
import scout_bounties as bounty
from strategic_claims import strategic_claim_text

STRATEGIC_CLAIM_MAX_AGE_DAYS = 365

SUPPLEMENTAL_CLAIM_PATTERNS = (
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
)

LinkedPrChecker = Callable[[Mapping[str, Any], str | None, list[dict[str, Any]]], str | None]
ClaimChecker = Callable[[Mapping[str, Any], list[dict[str, Any]]], str | None]
SupplementalClaimChecker = Callable[[Mapping[str, Any], list[dict[str, Any]]], str | None]
SearchPrChecker = Callable[[Mapping[str, Any], str | None, list[dict[str, Any]]], str | None]


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

    _, number = bounty.issue_repo_and_number(item)
    for comment in comments:
        if not claim_source_is_recent(comment):
            continue
        body = str(comment.get("body", ""))
        author = str((comment.get("user") or {}).get("login", "someone"))
        if strategic_claim_text(body):
            return f"active claim by @{author}"

        if number:
            branch = re.search(
                rf"https://github\.com/([^/\s]+)/[^/\s]+/tree/"
                rf"[^\s)]*(?:issue|fix)[-_/]?{number}\b",
                body,
                re.IGNORECASE,
            )
            if branch and branch.group(1).lower() == author.lower():
                return f"active implementation branch linked by @{author}"
    return None


def linked_open_pr_reason(
    item: Mapping[str, Any],
    token: str | None,
    comments: list[dict[str, Any]],
) -> str | None:
    """Detect explicit implementation PR links in the issue body or comments."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number:
        return None

    repo_pattern = re.escape(repo)
    candidates: list[str] = []

    issue_body = str(item.get("body", ""))
    for match in re.finditer(
        rf"https://github\.com/{repo_pattern}/pull/(\d+)",
        issue_body,
        re.IGNORECASE,
    ):
        start = max(0, match.start() - 160)
        end = min(len(issue_body), match.end() + 160)
        context = issue_body[start:end]
        if re.search(
            r"\b(?:fix(?:es|ed|ing)?|implementation|patch|solution|"
            r"address(?:es|ed|ing)?|resolv(?:es|ed|ing)?)\b",
            context,
            re.IGNORECASE,
        ):
            candidates.append(match.group(1))

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
        candidates.extend(
            re.findall(
                r"\b(?:related\s+)?(?:draft\s+)?"
                r"(?:fix|patch|implementation|pr|pull request)"
                r"(?:\s+(?:is\s+)?(?:in|at))?\s*[:(]?\s*#(\d+)\b",
                body,
                re.IGNORECASE,
            )
        )

    for pr_number in dict.fromkeys(candidates):
        pr = github.github_get(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
            token,
        )
        if isinstance(pr, dict) and pr.get("state") == "open":
            url = pr.get("html_url") or f"https://github.com/{repo}/pull/{pr_number}"
            return f"existing open implementation PR: {url}"

    return None


def search_open_implementation_pr_reason(
    item: Mapping[str, Any],
    token: str | None,
    comments: list[dict[str, Any]],
) -> str | None:
    """Search for an open PR only when the thread hints that implementation exists."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number:
        return None

    context = "\n".join(
        [str(item.get("body", ""))] + [str(comment.get("body", "")) for comment in comments]
    )
    if not re.search(
        r"\b(?:pr|pull request|draft fix|draft patch|"
        r"implementation (?:pr|pull request|fix|patch))\b",
        context,
        re.IGNORECASE,
    ):
        return None

    params = urllib.parse.urlencode(
        {
            "q": f"repo:{repo} is:pr is:open {number}",
            "per_page": 10,
        }
    )
    data = github.github_get(f"https://api.github.com/search/issues?{params}", token)
    if not isinstance(data, dict):
        return "could not verify open implementation PR search"

    repo_pattern = re.escape(repo)
    issue_ref = rf"(?:#{number}\b|https://github\.com/{repo_pattern}/issues/{number}\b)"
    closes_issue = re.compile(
        rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
        rf"\s*:?\s*(?:issue\s+)?{issue_ref}",
        re.IGNORECASE,
    )
    for candidate in data.get("items") or []:
        if not isinstance(candidate, dict) or not candidate.get("pull_request"):
            continue
        text = f"{candidate.get('title', '')}\n{candidate.get('body', '')}"
        if not closes_issue.search(text):
            continue
        url = candidate.get("html_url")
        if url:
            return f"existing open implementation PR: {url}"

    return None


def timeline_open_pr_reason(item: Mapping[str, Any], token: str | None) -> str | None:
    """Detect open PRs that GitHub has cross-referenced on the issue timeline."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number:
        return None

    timeline = github.github_get(
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
    item: Mapping[str, Any],
    comments: list[dict[str, Any]],
) -> str | None:
    """Detect clear work claims not covered by the upstream scanner."""
    if not int(item.get("comments") or 0):
        return None

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
    comments: list[dict[str, Any]],
    *,
    linked_pr_checker: LinkedPrChecker = linked_open_pr_reason,
    supplemental_claim_checker: SupplementalClaimChecker = supplemental_claim_reason,
) -> str | None:
    """Apply paid-compatible competition checks using supplied issue comments."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number:
        return "could not identify repository/issue number"

    reason = bounty.has_existing_implementation_pr(repo, number, token)
    if reason:
        return reason

    reason = linked_pr_checker(item, token, comments)
    if reason:
        return reason

    for comment in comments:
        body = str(comment.get("body", ""))
        for pattern in bounty.CLAIM_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                author = (comment.get("user") or {}).get("login", "someone")
                return f"active claim by @{author}"

    return supplemental_claim_checker(item, comments)


def strategic_competition_reason(
    item: Mapping[str, Any],
    token: str | None,
    comments: list[dict[str, Any]],
    *,
    linked_pr_checker: LinkedPrChecker = linked_open_pr_reason,
    strategic_claim_checker: ClaimChecker = strategic_claim_reason,
    search_pr_checker: SearchPrChecker = search_open_implementation_pr_reason,
) -> str | None:
    """Apply strategic-only competition checks without changing paid-bounty behavior."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number:
        return "could not identify repository/issue number"

    reason = bounty.has_existing_implementation_pr(repo, number, token)
    if reason:
        return reason

    reason = linked_pr_checker(item, token, comments)
    if reason:
        return reason

    reason = strategic_claim_checker(item, comments)
    if reason:
        return reason

    return search_pr_checker(item, token, comments)
