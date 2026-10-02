from __future__ import annotations

import unittest
from typing import Any, cast

from bountyscout import paid
from bountyscout.types import GitHubIssue


def issue(**overrides: Any) -> GitHubIssue:
    base: dict[str, Any] = {
        "html_url": "https://github.com/acme/widget/issues/42",
        "title": "Fix bug",
        "body": "",
        "labels": [],
        "assignees": [],
        "comments": 0,
    }
    base.update(overrides)
    return cast(GitHubIssue, base)


class PaymentSignalTests(unittest.TestCase):
    def test_constants_preserve_paid_policy(self) -> None:
        self.assertEqual(paid.MAX_COMMENTS, 25)
        self.assertEqual(
            paid.PAYMENT_TERM_RE,
            r"(?:bounty|reward|payout|compensation|pay(?:ment|s|ing|s)?|paid)",
        )
        self.assertEqual(
            paid.AMOUNT_RE,
            (
                r"(?:[$€£]\s*\d[\d,]*(?:\.\d+)?|"
                r"(?<!ERC-)(?<!\d)\d+(?:\.\d+)?\s*"
                r"(?:usd|usdc|usdt|eur|gbp|xmr|sol|eth|btc)\b)"
            ),
        )

    def test_payment_signal_precedence_and_strings(self) -> None:
        cases = [
            (issue(body="/bounty $25"), "explicit bounty command: $25"),
            (issue(body="reward available: 50 USDC"), "payment term + amount: 50 USDC"),
            (issue(body="$75 compensation after merge"), "amount + payment term: $75"),
            (
                issue(body="", labels=[{"name": "bounty"}, "$40"]),
                "bounty labels: $40",
            ),
            (
                issue(body="Funded through Opire", title="Task"),
                "named bounty platform + funding language",
            ),
            (issue(body="20 USDC bounty", title="Task"), "amount + payment term: 20 USDC"),
            (issue(body="$20 bounty", title="Task"), "amount + payment term: $20"),
        ]
        for item, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(paid.payment_signal(item), expected)

    def test_payment_signal_negative_regressions(self) -> None:
        chain_love_body = (
            "No payout is assumed unless the DBIP is approved through the official "
            "Chain.Love compensation process.\n\n"
            "### Rewards address\n\n"
            "Will provide an Ethereum-mainnet ERC-20 USDC/USDT address upon approval "
            "if required."
        )
        cases = [
            issue(body="Reward address: ERC-20 USDC", title="Task"),
            issue(body=chain_love_body, title="[DBIP] Proposal"),
            issue(body="bounty maybe someday", title="Task"),
            issue(labels=["$40"]),
            issue(body="Algora integration", title="Task"),
        ]
        for item in cases:
            with self.subTest(item=item):
                self.assertIsNone(paid.payment_signal(item))


class EligibilityTests(unittest.TestCase):
    def test_clean_candidate_acceptance_and_comment_boundary(self) -> None:
        self.assertTrue(paid.is_clean_candidate(issue()))
        self.assertTrue(paid.is_clean_candidate(issue(comments=paid.MAX_COMMENTS)))

    def test_clean_candidate_rejections(self) -> None:
        generated_markers = [
            "bounty alert:",
            "active bounty scan results",
            "new opportunities found",
            "new opportunityies found",
        ]
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
        cases = [
            issue(pull_request={}),
            issue(html_url="https://github.com/laclance/BountyScout/issues/1"),
            issue(assignees=[{"login": "dev"}]),
            issue(comments=paid.MAX_COMMENTS + 1),
            issue(body="Active bounty scan results"),
            issue(body="article writing proposal"),
        ]
        cases.extend(issue(title=marker) for marker in generated_markers)
        cases.extend(issue(title=term) for term in blocklist)
        for item in cases:
            with self.subTest(item=item):
                self.assertFalse(paid.is_clean_candidate(item))


if __name__ == "__main__":
    unittest.main()
