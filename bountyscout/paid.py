"""Paid-opportunity eligibility and payment-signal policy.

This module operates only on already-fetched GitHub issue evidence.
No network I/O belongs here.
"""

from __future__ import annotations

import re
from typing import Final

from bountyscout.types import GitHubIssue

MAX_COMMENTS: Final = 25
PAYMENT_TERM_RE: Final = r"(?:bounty|reward|payout|compensation|pay(?:ment|s|ing|s)?|paid)"
AMOUNT_RE: Final = (
    r"(?:[$€£]\s*\d[\d,]*(?:\.\d+)?|"
    r"(?<!ERC-)(?<!\d)\d+(?:\.\d+)?\s*(?:usd|usdc|usdt|eur|gbp|xmr|sol|eth|btc)\b)"
)


def payment_signal(item: GitHubIssue) -> str | None:
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


def is_clean_candidate(item: GitHubIssue) -> bool:
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
