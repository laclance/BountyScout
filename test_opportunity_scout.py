import unittest
from unittest.mock import patch

import opportunity_scout as scout


def issue(comments=1):
    return {
        "html_url": "https://github.com/example/project/issues/42",
        "comments": comments,
        "title": "Fix deterministic regression",
        "body": "",
        "labels": [],
    }


class PaymentSignalTests(unittest.TestCase):
    def test_algora_confirmation_is_high_confidence(self):
        comments = [{
            "body": (
                "💎 **$25** bounty created by @maintainer\n"
                "Submit your pull request on https://console.algora.io/bounties/x"
            ),
            "user": {"login": "algora-pbc"},
            "author_association": "NONE",
        }]
        with patch.object(scout.bounty, "github_get", return_value=comments):
            signal = scout.comment_payment_signal(issue(), "token")
        self.assertEqual(
            signal,
            "confirmed bounty platform comment (Algora): $25",
        )
        self.assertEqual(scout.payment_confidence(signal), 100)

    def test_bountyhub_confirmation_does_not_require_maintainer_author(self):
        comments = [{
            "body": (
                "🚀 A bounty of $100 has been created by @sponsor on BountyHub. "
                "https://bountyhub.dev/example"
            ),
            "user": {"login": "bountyhub-bot"},
            "author_association": "NONE",
        }]
        with patch.object(scout.bounty, "github_get", return_value=comments):
            signal = scout.comment_payment_signal(issue(), "token")
        self.assertEqual(
            signal,
            "confirmed bounty platform comment (BountyHub): $100",
        )

    def test_trusted_opire_reward_command(self):
        comments = [{
            "body": "/reward 75",
            "user": {"login": "maintainer"},
            "author_association": "OWNER",
        }]
        with patch.object(scout.bounty, "github_get", return_value=comments):
            signal = scout.comment_payment_signal(issue(), "token")
        self.assertEqual(signal, "explicit /reward comment: $75")

    def test_untrusted_reward_command_is_ignored(self):
        comments = [{
            "body": "/reward 9999",
            "user": {"login": "random-user"},
            "author_association": "NONE",
        }]
        with patch.object(scout.bounty, "github_get", return_value=comments):
            self.assertIsNone(scout.comment_payment_signal(issue(), "token"))

    def test_supplemental_cash_prize_signal(self):
        item = issue(comments=0)
        item["body"] = "Cash prize: $50 for the merged fix."
        self.assertEqual(
            scout.supplemental_payment_signal(item),
            "explicit paid-work wording: $50",
        )

    def test_zar_symbol_does_not_match_pr_number(self):
        self.assertIsNone(scout.re.search(scout.EXTENDED_AMOUNT_RE, "PR 123"))
        self.assertIsNotNone(scout.re.search(scout.EXTENDED_AMOUNT_RE, "R 250"))


class PlatformParserTests(unittest.TestCase):
    def test_issuehunt_listing_maps_to_github_issue(self):
        html = (
            '<a href="/r/apache/superset/issues/3821">Funded #3821</a>'
            '<span>$17.00</span>'
        )
        with patch.object(scout, "fetch_text", return_value=html):
            refs = scout.issuehunt_platform_refs()
        self.assertEqual(
            refs["https://github.com/apache/superset/issues/3821"],
            "confirmed bounty platform feed (IssueHunt): $17.00",
        )

    def test_opire_detail_maps_to_github_issue(self):
        pages = {
            "https://app.opire.dev/home": '<a href="/issues/ABC123">bounty</a>',
            "https://app.opire.dev/issues/ABC123": (
                "$50 bounty for task "
                "https://github.com/example/project/issues/42"
            ),
        }
        with patch.object(scout, "fetch_text", side_effect=pages.get):
            refs = scout.opire_platform_refs()
        self.assertEqual(
            refs["https://github.com/example/project/issues/42"],
            "confirmed bounty platform feed (Opire): $50",
        )

    def test_bountyhub_detail_maps_to_github_issue(self):
        pages = {
            "https://www.bountyhub.dev/en/bounties": (
                '<a href="/en/bounty/view/abc-123">view</a>'
            ),
            "https://www.bountyhub.dev/en/bounty/view/abc-123": (
                "Reward $125 "
                "https://github.com/example/project/issues/42"
            ),
        }
        with patch.object(scout, "fetch_text", side_effect=pages.get):
            refs = scout.bountyhub_platform_refs()
        self.assertEqual(
            refs["https://github.com/example/project/issues/42"],
            "confirmed bounty platform feed (BountyHub): $125",
        )


if __name__ == "__main__":
    unittest.main()
