"""Pure strategic readiness and issue-lifecycle policy.

These helpers interpret issue metadata and maintainer comments. They perform no
network I/O and do not depend on the application orchestrator.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from strategic_claims import normalized_claim_text

TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def _labels_text(item: Mapping[str, Any]) -> str:
    """Return issue labels as normalized lowercase text."""
    return " ".join(
        str(label.get("name", "")) if isinstance(label, dict) else str(label)
        for label in (item.get("labels") or [])
    ).lower()


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
    title = str(item.get("title", ""))
    normalized_labels = re.sub(r"[-_/:]+", " ", _labels_text(item))
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


def _diagnostic_evidence_supplied(body: str) -> bool:
    """Return whether a commenter supplied evidence previously requested by maintainers."""
    return bool(
        re.search(
            r"\b(?:attached|provided|uploaded|included|here(?:'s| is)|see)\b"
            r".{0,160}\b(?:cpu profile|memory profile|heap profile|profile|stack trace|"
            r"minimal reproducer|reproducer|logs?|benchmark|trace|dump)\b",
            body,
            re.DOTALL,
        )
    )


def _diagnostic_requested(body: str) -> bool:
    """Return whether a maintainer explicitly requested concrete diagnostic evidence."""
    request = re.search(
        r"\b(?:could|can|would)\s+you\s+(?:please\s+)?"
        r"(?:provide|share|attach|capture|collect|send)\b|"
        r"\bwould\s+it\s+be\s+possible\s+to\s+(?:provide|share|attach)\b|"
        r"\bplease\s+(?:provide|share|attach|capture|collect|send)\b|"
        r"\bwe\s+need\s+(?:a|the)\b",
        body,
    )
    evidence = re.search(
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
    return bool(request and evidence)


def _redirects_to_other_project(body: str) -> bool:
    """Return whether the maintainer redirects the actual change to another project."""
    redirect_target = bool(
        re.search(
            r"https://github\.com/[\w.-]+/[\w.-]+|"
            r"(?<![\w.-])[\w.-]+/[\w.-]+(?![\w.-])|"
            r"\b(?:specification|upstream|another repository|another project|"
            r"canonical repository)\b",
            body,
        )
    )
    if not redirect_target:
        return False
    return bool(
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


def _canonical_duplicate(body: str) -> bool:
    """Return whether a maintainer points to another issue as the canonical tracker."""
    canonical_reference = bool(
        re.search(
            r"(?<!\w)#\d+\b|https://github\.com/[\w.-]+/[\w.-]+/issues/\d+",
            body,
        )
    )
    if not canonical_reference or "not a duplicate" in body:
        return False
    return bool(
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


def _explicit_ready_signal(body: str) -> bool:
    """Return whether a maintainer explicitly says implementation may proceed."""
    return any(
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


def _maintainer_hold_reason(body: str, proposal_stage: bool) -> tuple[str | None, bool]:
    """Return a not-ready reason and whether it represents pending diagnostics."""
    diagnostic_requested = _diagnostic_requested(body)
    if diagnostic_requested:
        return "maintainer is waiting for requested diagnostic evidence", True
    if _redirects_to_other_project(body):
        return "maintainer redirected implementation/discussion to another project", False
    if _canonical_duplicate(body):
        return "maintainer indicates this is probably tracked by another canonical issue", False
    if any(
        marker in body
        for marker in (
            "would be the wrong solution",
            "is the wrong solution",
            "would be the wrong approach",
            "is the wrong approach",
            "not the right solution",
            "not the right approach",
            "should not be implemented",
            "shouldn't be implemented",
        )
    ):
        return "maintainer indicates the proposed implementation approach is not wanted", False
    if any(
        marker in body
        for marker in (
            "needs discussion",
            "need more discussion",
            "need to discuss this first",
            "should discuss this first",
        )
    ):
        return "maintainer says issue still needs discussion", False
    if any(
        marker in body
        for marker in (
            "needs investigation",
            "need more investigation",
            "need to investigate",
            "needs engineering investigation",
        )
    ):
        return "maintainer says issue still needs investigation", False
    if any(
        marker in body
        for marker in (
            "needs reproduction",
            "need a reproduction",
            "need reproduction",
            "please reproduce",
            "can you reproduce",
        )
    ) or ("are you sure" in body and "reproduc" in body):
        return "maintainer says reproduction is still required", False
    if any(
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
        return "maintainer asked contributors to wait before implementation", False
    if any(
        marker in body
        for marker in (
            "needs clarification",
            "need clarification",
            "need to clarify",
            "please clarify before",
        )
    ):
        return "maintainer says issue still needs clarification", False
    if proposal_stage and any(
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
        return "proposal is still gathering feedback", False
    if proposal_stage and "time-boxed" in body and "discussion" in body:
        return "proposal is still gathering feedback", False
    return None, False


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

        if diagnostic_pending and _diagnostic_evidence_supplied(body):
            state = None
            reason = None
            diagnostic_pending = False

        if not maintainer_comment_authority(comment):
            continue

        if _explicit_ready_signal(body):
            state = True
            reason = None
            diagnostic_pending = False
            continue

        hold_reason, diagnostic_requested = _maintainer_hold_reason(body, proposal_stage)
        if hold_reason:
            state = False
            reason = hold_reason
            diagnostic_pending = diagnostic_requested

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


def maintainer_issue_decision_reason(item: Mapping[str, Any]) -> str | None:
    """Reject trusted maintainer-authored issues that explicitly remain in decision stage."""
    association = str(item.get("author_association", "")).upper()
    if association not in TRUSTED_ASSOCIATIONS:
        return None

    body = str(item.get("body", "")).lower()
    if re.search(
        r"\b(?:this|the) issue is to decide\b|"
        r"\bwe (?:still )?need to decide\b|"
        r"\bdecision (?:is|remains) (?:open|pending)\b",
        body,
    ):
        return "maintainer-authored issue is still deciding implementation semantics"
    return None


def automated_tracking_issue_reason(item: Mapping[str, Any]) -> str | None:
    """Reject bot-maintained dashboards/trackers that are not contributor tasks."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = _labels_text(item)
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

    body_text = body.lower()
    ci_incident = (
        "nightly-failure" in labels
        or "this issue closes itself on the next successful" in body_text
        or bool(
            re.search(
                r"\bnext successful\s+(?:build|publish|publication|run)"
                r"\s+will\s+(?:update|close)\s+this\s+(?:issue|incident)\b",
                body_text,
            )
        )
    )
    if ci_incident:
        return "automated CI/release incident, not an implementation task"
    return None


def release_tracking_reason(
    item: Mapping[str, Any],
    comments: list[dict[str, Any]] | None = None,
) -> str | None:
    """Reject release bookkeeping and work that is already implemented."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
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
            r"\b(?:fixed|implemented|resolved)\s+(?:on|in)\s+(?:the\s+)?(?:main|master)\b",
            r"\b(?:fix|implementation)\s+(?:is|has\s+been)\s+(?:already\s+)?(?:on|in)\s+(?:main|master)\b",
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
            r"\b(?:not|isn't|is\s+not|hasn't\s+been|has\s+not\s+been)\s+released\s+yet\b",
            r"\b(?:awaiting|waiting\s+for)\s+(?:the\s+)?next\s+release\b",
            r"\b(?:will|should)\s+be\s+(?:good\s+)?(?:in|with)\s+(?:the\s+)?next\s+release\b",
            r"\b(?:will|should)\s+be\s+included\s+in\s+(?:the\s+)?next\s+release\b",
        )
    )
    if implementation_done and release_only:
        return "implementation already merged; only release/tagging remains"
    return None
