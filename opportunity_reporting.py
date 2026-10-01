"""Reporting and notification formatting for OSS Opportunity Scout.

This module is deliberately presentation-only: it renders already-ranked candidate,
rejection, and audit data without performing network I/O or scanner policy decisions.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Mapping, Sequence


def github_report_ref(text: Any) -> str:
    """Make GitHub issue/PR URLs clickable without creating backlinks."""
    value = str(text or "")
    return re.sub(
        r"https://github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)",
        lambda match: (
            "https://redirect.github.com/"
            f"{match.group(1)}/{match.group(2)}/{match.group(3)}/{match.group(4)}"
        ),
        value,
        flags=re.IGNORECASE,
    )


def markdown_label(text: Any) -> str:
    """Escape text used inside a Markdown link label."""
    value = github_report_ref(text)
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def strategic_priority_delta(candidate: Mapping[str, Any]) -> int:
    """Return the execution adjustment applied on top of career score."""
    return int(candidate["priority_score"]) - int(candidate["career_score"])


def markdown_candidate(candidate: Mapping[str, Any], idx: int) -> str:
    """Render one paid or strategic candidate for the GitHub queue issue."""
    hourly = (
        "$" + f"{candidate['expected_hourly']:.0f}/h"
        if candidate["expected_hourly"] is not None
        else "unknown / not USD-comparable"
    )
    if candidate["expected_hourly"] is not None:
        hourly = "~" + hourly
    guide = (
        f"[contribution guide]({candidate['contribution_guide']})"
        if candidate["contribution_guide"]
        else "not found at common paths"
    )
    title = markdown_label(candidate.get("title"))
    repo = markdown_label(candidate.get("repo"))
    lines = [
        f"#### {idx}. [{repo} #{candidate['issue_number']}]"
        f"({github_report_ref(candidate['url'])}): {title}",
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
        delta = strategic_priority_delta(candidate)
        delta_text = f"+{delta}" if delta >= 0 else str(delta)
        lines.extend(
            [
                f"- **Career score:** {candidate['career_score']}/100",
                f"- **Priority score:** {candidate['priority_score']}/100",
                f"- **Execution adjustment:** {delta_text} "
                f"(career {candidate['career_score']} → priority {candidate['priority_score']})",
                f"- **Effort:** {candidate['effort']}",
            ]
        )

    effort_reasons = candidate.get("effort_reasons") or []
    if effort_reasons:
        lines.append(f"- **Effort basis:** {', '.join(str(x) for x in effort_reasons)}")

    priority_reasons = candidate.get("priority_reasons") or []
    if priority_reasons and not candidate["paid"]:
        lines.append(f"- **Priority basis:** {', '.join(str(x) for x in priority_reasons)}")

    labels = candidate.get("labels") or []
    lines.extend(
        [
            f"- **Competition:** {candidate['competition']}",
            f"- **Repo stars:** {candidate['stars']}",
            f"- **Repo recent activity:** {candidate['recent_activity']}",
            f"- **Language:** {candidate['language']}",
            f"- **Labels:** {', '.join(str(x) for x in labels) or 'none'}",
            f"- **Contribution process:** {guide}",
        ]
    )
    if candidate["paid"]:
        lines.append(f"- **Cash reasons:** {', '.join(str(x) for x in candidate['cash_reasons'])}")
    else:
        lines.append(
            f"- **Career reasons:** {', '.join(str(x) for x in candidate['career_reasons'])}"
        )
    return "\n".join(lines) + "\n\n"


def notification_candidate(candidate: Mapping[str, Any], idx: int) -> list[str]:
    """Render one concise notification entry."""
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
        lines.append(
            f"   • strategic OSS | career: {candidate['career_score']}/100 | "
            f"priority: {candidate['priority_score']}/100"
        )
    lines.extend(
        [
            f"   • {candidate['effort']} | competition: {candidate['competition']}",
            f"   • {candidate['url']}",
        ]
    )
    return lines


def notification_message(
    queue: Sequence[Mapping[str, Any]],
    now: str,
    *,
    max_chars: int = 1900,
) -> str:
    """Render a notification that stays within the stricter Discord text budget."""
    header = f"🎯 *OSS Opportunity Queue* ({now})\n\n"
    parts: list[str] = []
    omitted = 0

    for idx, candidate in enumerate(queue, 1):
        block = "\n".join(notification_candidate(candidate, idx)) + "\n\n"
        remaining = len(queue) - idx
        proposed = header + "".join(parts) + block
        if len(proposed) <= max_chars:
            parts.append(block)
            continue
        omitted = remaining + 1
        break

    message = header + "".join(parts)
    if omitted:
        suffix = f"… {omitted} more ranked candidate(s) in the GitHub report."
        room = max_chars - len(suffix) - 1
        message = message[: max(0, room)].rstrip() + "\n" + suffix
    return message.rstrip()


def _reason_summary(counts: Mapping[str, int], *, limit: int = 6) -> str:
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return "; ".join(f"{reason} ×{count}" for reason, count in ordered[:limit])


def rejection_summary(
    paid_rejects: Mapping[str, int],
    strategic_rejects: Mapping[str, int],
) -> dict[str, int]:
    """Combine paid/strategic reject counts for reporting."""
    combined = dict(paid_rejects)
    for reason, count in strategic_rejects.items():
        combined[reason] = combined.get(reason, 0) + count
    return combined


def markdown_examples(
    heading: str,
    examples: Sequence[Mapping[str, Any]],
    *,
    limit: int = 12,
) -> str:
    """Render linked reject/audit examples without creating source backlinks."""
    if not examples:
        return ""
    lines = [f"### {heading}", ""]
    for item in examples[:limit]:
        source = github_report_ref(item.get("url"))
        title = markdown_label(item.get("title") or source)
        reason = github_report_ref(item.get("reason"))
        lines.append(f"- [{title}]({source}): {reason}")
    return "\n".join(lines) + "\n"


def audit_summary(audit: Sequence[Mapping[str, Any]]) -> str:
    """Summarize recurring tuning signals before listing concrete examples."""
    if not audit:
        return ""
    counts = Counter(str(item.get("reason") or "unknown audit reason") for item in audit)
    return _reason_summary(counts)


def github_report_body(
    queue: Sequence[Mapping[str, Any]],
    now: str,
    *,
    verification_examples: Sequence[Mapping[str, Any]] = (),
    strategic_audit: Sequence[Mapping[str, Any]] = (),
    reject_counts: Mapping[str, int] | None = None,
) -> str:
    """Render the full GitHub queue report."""
    body = (
        f"### Ranked OSS Opportunity Queue\n\n**Scan Time:** {now}\n\n"
        "Paid candidates reuse BountyScout's existing payment/competition filters unchanged. "
        "Strategic candidates are pre-ranked, then source-refreshed and checked for assignees, "
        "claim comments, open implementation PRs, repository legitimacy, and contribution guidance. "
        "For strategic work, career score measures long-term value while priority score applies "
        "execution friction from effort and visible competition.\n\n"
    )
    for idx, candidate in enumerate(queue, 1):
        body += markdown_candidate(candidate, idx)

    if reject_counts:
        total = sum(reject_counts.values())
        body += "### Verification summary\n\n"
        body += f"**Filtered candidates:** {total}\n\n"
        body += f"**Top rejection reasons:** {_reason_summary(reject_counts)}\n\n"

    body += markdown_examples("Verification rejects", verification_examples)

    if strategic_audit:
        body += "\n### Potential scanner misses / tuning candidates\n\n"
        body += f"**Audit summary:** {audit_summary(strategic_audit)}\n\n"
        for item in strategic_audit[:12]:
            source = github_report_ref(item.get("url"))
            title = markdown_label(item.get("title") or source)
            reason = github_report_ref(item.get("reason"))
            body += f"- [{title}]({source}): {reason}\n"

    return body


def github_report_title(queue_size: int) -> str:
    """Return the GitHub issue title for a queue run."""
    suffix = "s" if queue_size != 1 else ""
    return f"🎯 OSS Opportunity Queue: {queue_size} new verified candidate{suffix}"
