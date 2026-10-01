"""Opportunity scoring and effort estimation.

Owns ranking math and implementation-effort heuristics over already-fetched evidence.
No network I/O belongs in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Collection, Mapping

import scout_bounties as bounty
from strategic_readiness import TRUSTED_ASSOCIATIONS


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


def _prose_body(body: str) -> str:
    """Remove fenced diagnostics/code so dump size does not masquerade as implementation scope."""
    fenced = re.compile(r"\x60{3}.*?\x60{3}|~~~.*?~~~", re.DOTALL)
    return re.sub(r"\s+", " ", fenced.sub(" ", body)).strip()


def _normalized_labels(labels: str) -> str:
    return re.sub(r"[-_/:]+", " ", labels.lower())


def _feature_signal(title: str, labels: str, text: str) -> bool:
    normalized_labels = _normalized_labels(labels)
    return bool(
        title.lower().startswith(("fr:", "feature request:"))
        or any(
            marker in normalized_labels
            for marker in (
                "kind feature",
                "type feature",
                "feature request",
                "enhancement",
            )
        )
        or re.search(r"\bfeature request\b", text)
    )


def _cross_component_feature(text: str, file_refs: int) -> bool:
    """Identify features that span several persistence/configuration/runtime concerns."""
    if file_refs >= 3:
        return True
    component_terms = (
        "schema",
        "storage",
        "bucket",
        "chunks",
        "index",
        "compactor",
        "configuration",
        "protocol",
        "wire format",
        "database",
        "migration",
        "snapshot",
        "wal",
        "handshake",
    )
    return sum(term in text for term in component_terms) >= 3


@dataclass(frozen=True)
class EffortEstimate:
    """Bucketed implementation estimate plus concise calibration reasons."""

    bucket: str
    reasons: tuple[str, ...]


def estimate_effort_details(item: Mapping[str, Any]) -> EffortEstimate:
    """Estimate implementation effort while separating scope from discussion volume."""
    title, body, labels, text = issue_text(item)
    prose = _prose_body(body)
    file_refs = code_reference_count(body)
    feature = _feature_signal(title, labels, text)

    if documentation_microfix(item):
        return EffortEstimate("<1h", ("documentation-only micro-fix",))

    explicit_large_scope = bool(
        re.search(
            r"\b(?:epic|roadmap|redesign|rewrite|multi-phase|architecture|"
            r"large refactor|rfc|connection pool|explore publishing)\b",
            text,
        )
        or title.lower().startswith(("fr:", "feature request:"))
        or "feature request" in _normalized_labels(labels)
    )
    compatibility_risk = bool(
        re.search(
            r"\b(?:backward[- ]incompatible|backwards? compatibility|"
            r"compatibility (?:risk|break|constraint)|persisted (?:state|data)|"
            r"existing deployments?|wire format|on-disk format)\b",
            text,
        )
        or (
            re.search(r"\b(?:wal|snapshot|handshake)\b", text)
            and re.search(r"\b(?:persist|compatib|existing cluster|rejoin|recover)\w*\b", text)
        )
    )
    environment_heavy = bool(
        re.search(
            r"\b(?:dual[ -]?sim|physical device|device-specific|hardware-dependent)\b",
            text,
        )
        or re.search(
            r"\b(?:unable to reproduce|cannot reproduce|can't reproduce|"
            r"haven't been able to reproduce|have not been able to reproduce|"
            r"low-probability race|non[- ]deterministic repro)\b",
            text,
        )
    )

    if explicit_large_scope:
        return EffortEstimate("1d+", ("explicit broad feature/design scope",))
    if compatibility_risk:
        return EffortEstimate("1d+", ("backward-compatibility or persisted-state risk",))
    if environment_heavy:
        return EffortEstimate("1d+", ("environment/reproduction-heavy investigation",))
    if feature and _cross_component_feature(text, file_refs):
        return EffortEstimate("1d+", ("feature spans multiple runtime/configuration components",))
    if len(prose) > 12000:
        return EffortEstimate("1d+", ("large narrative implementation scope",))

    docs_signal = bool(
        re.search(r"\b(?:docs?|documentation|readme)\b", f"{title}\n{labels}", re.IGNORECASE)
    )
    if docs_signal and file_refs == 0 and len(prose) < 4500:
        return EffortEstimate("1–3h", ("bounded documentation change",))

    concurrency_risk = bool(
        re.search(r"\b(?:data race|race condition|deadlock|concurren\w*)\b", text)
    )
    upstream_dependency = bool(
        re.search(
            r"\b(?:may be related to|upstream (?:issue|dependency)|"
            r"vendor(?:ed)? dependency|third[- ]party dependency)\b",
            text,
        )
    )
    suggested_fix_bullets = len(re.findall(r"(?m)^\s*-\s+", body))
    mobile_or_desktop = any(
        marker in labels.lower() for marker in ("os-android", "os-ios", "os-macos", "os-windows")
    )
    missing_reproduction = "_no response_" in text or "no response" in text

    if (
        file_refs >= 4
        or feature
        or len(prose) > 6500
        or (mobile_or_desktop and missing_reproduction)
        or (re.search(r"\bsuggested fix(?:es)?\b", text) and suggested_fix_bullets >= 3)
    ):
        reasons: list[str] = []
        if feature:
            reasons.append("feature/enhancement scope")
        if file_refs >= 4:
            reasons.append("multiple referenced files")
        if len(prose) > 6500:
            reasons.append("large narrative scope")
        if suggested_fix_bullets >= 3:
            reasons.append("multi-step suggested implementation")
        if mobile_or_desktop and missing_reproduction:
            reasons.append("platform-specific reproduction is missing")
        return EffortEstimate("6–12h", tuple(reasons[:3]) or ("broader implementation scope",))

    if concurrency_risk:
        return EffortEstimate("3–6h", ("concurrency/lifecycle debugging risk",))
    if upstream_dependency:
        return EffortEstimate("3–6h", ("upstream/dependency investigation",))

    localized_todo = bool(
        file_refs <= 2
        and re.search(r"\btodo\b", text)
        and re.search(r"\b(?:method|function|handler|header|path|codebase)\b", text)
    )
    bounded = bool(
        re.search(
            r"\b(?:regression|deterministic|panics?|segfault|nil pointer|"
            r"leaks?|incorrect|failing tests?|unit tests?|single|small|narrow|"
            r"no-op|stale)\b|\bnever closes\b|\bevery sync\b",
            f"{title.lower()} {labels} {text[:4500]}",
        )
    )
    if (bounded or localized_todo) and len(prose) < 4500 and file_refs <= 2:
        reason = (
            "localized TODO/code-path change"
            if localized_todo
            else "bounded deterministic bug signal"
        )
        return EffortEstimate("1–3h", (reason,))

    return EffortEstimate("3–6h", ("moderate implementation scope",))


def estimate_effort(item: Mapping[str, Any]) -> str:
    """Return the public effort bucket for an issue."""
    return estimate_effort_details(item).bucket


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


def reward_text(signal: str | None, amount_pattern: str) -> str | None:
    if not signal:
        return None
    match = re.search(amount_pattern, signal, re.IGNORECASE)
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


def strategic_priority_score(
    career_score: int,
    effort: str,
    competition_level: str,
) -> tuple[int, list[str]]:
    """Turn career value into actionable priority using execution friction.

    Career score remains the long-term value signal. Priority answers the more
    practical question: given similarly valuable issues, which one is the best
    use of contributor time right now?
    """
    effort_adjustment = {
        "<1h": 5,
        "1–3h": 6,
        "3–6h": 2,
        "6–12h": -4,
        "1d+": -10,
    }[effort]
    competition_adjustment = {
        "none": 7,
        "low": 3,
        "medium": -4,
        "high": -10,
    }[competition_level]

    reasons: list[str] = []
    if effort_adjustment > 0:
        reasons.append(f"{effort} execution bonus")
    else:
        reasons.append(f"{effort} execution penalty")

    if competition_level == "none":
        reasons.append("no visible competition bonus")
    elif competition_adjustment > 0:
        reasons.append(f"{competition_level} competition bonus")
    else:
        reasons.append(f"{competition_level} competition penalty")

    priority = max(
        0,
        min(100, career_score + effort_adjustment + competition_adjustment),
    )
    return priority, reasons


def build_candidate(
    item: Mapping[str, Any],
    lane: str,
    signal: str | None,
    repo_meta: Mapping[str, Any],
    guide: str | None,
    activity_comments: list[dict[str, Any]] | None = None,
    *,
    target_repos: Collection[str],
    amount_pattern: str,
) -> dict[str, Any]:
    repo, number = bounty.issue_repo_and_number(item)
    effort_estimate = estimate_effort_details(item)
    effort = effort_estimate.bucket
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
    if repo in target_repos:
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

    if lane == "strategic":
        priority, priority_reasons = strategic_priority_score(career, effort, comp)
    else:
        priority = min(100, max(cash, career) + (5 if cash >= 70 and career >= 70 else 0))
        priority_reasons = []
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
        "reward": reward_text(signal, amount_pattern),
        "payment_confidence": payment_confidence(signal),
        "cash_score": cash,
        "career_score": career,
        "priority_score": priority,
        "priority_reasons": priority_reasons,
        "effort": effort,
        "effort_reasons": list(effort_estimate.reasons),
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
