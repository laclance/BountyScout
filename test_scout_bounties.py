import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import mock_open, patch

import scout_bounties as scout


def issue(**overrides):
    base = {
        "html_url": "https://github.com/acme/widget/issues/42",
        "title": "Fix deterministic regression",
        "body": "Paid bounty: $100",
        "labels": [],
        "assignees": [],
        "comments": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


class FakeResponse:
    def __init__(self, body=b"{}"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class StateTests(unittest.TestCase):
    def test_load_missing_returns_empty(self):
        with patch.object(scout.os.path, "exists", return_value=False):
            self.assertEqual(scout.load_seen_bounties(), set())

    def test_load_list_returns_set(self):
        with patch.object(scout.os.path, "exists", return_value=True), \
             patch("builtins.open", mock_open(read_data='["b", "a"]')):
            self.assertEqual(scout.load_seen_bounties(), {"a", "b"})

    def test_load_non_list_and_bad_json_return_empty(self):
        with patch.object(scout.os.path, "exists", return_value=True), \
             patch("builtins.open", mock_open(read_data='{"x": 1}')):
            self.assertEqual(scout.load_seen_bounties(), set())
        with patch.object(scout.os.path, "exists", return_value=True), \
             patch("builtins.open", mock_open(read_data='{')):
            self.assertEqual(scout.load_seen_bounties(), set())

    def test_save_success_sorts_and_failure_returns_false(self):
        handle = mock_open()
        with patch("builtins.open", handle):
            self.assertTrue(scout.save_seen_bounties({"b", "a"}))
        written = "".join(call.args[0] for call in handle().write.call_args_list)
        self.assertEqual(json.loads(written), ["a", "b"])

        with patch("builtins.open", side_effect=OSError("nope")):
            self.assertFalse(scout.save_seen_bounties({"a"}))


class HttpTests(unittest.TestCase):
    def test_github_get_success_and_failure(self):
        with patch.object(
            scout.urllib.request,
            "urlopen",
            return_value=FakeResponse(b'{"ok": true}'),
        ) as opened:
            self.assertEqual(scout.github_get("https://example", "tok", 3), {"ok": True})
            req = opened.call_args.args[0]
            self.assertEqual(req.headers["Authorization"], "Bearer tok")
            self.assertEqual(opened.call_args.kwargs["timeout"], 3)

        with patch.object(scout.urllib.request, "urlopen", side_effect=OSError("boom")):
            self.assertIsNone(scout.github_get("https://example"))

    def test_search_github_encodes_and_normalizes(self):
        with patch.object(scout, "github_get", return_value={"items": [1]}) as get:
            self.assertEqual(scout.search_github('a b', "t", per_page=7), {"items": [1]})
            self.assertIn("per_page=7", get.call_args.args[0])
            self.assertIn("q=a+b", get.call_args.args[0])
        with patch.object(scout, "github_get", return_value=[]):
            self.assertEqual(scout.search_github("x"), {})


class PaymentTests(unittest.TestCase):
    def test_payment_signal_variants(self):
        self.assertEqual(
            scout.payment_signal(issue(body="/bounty $25")),
            "explicit bounty command: $25",
        )
        self.assertEqual(
            scout.payment_signal(issue(body="reward available: 50 USDC")),
            "payment term + amount: 50 USDC",
        )
        self.assertEqual(
            scout.payment_signal(issue(body="$75 compensation after merge")),
            "amount + payment term: $75",
        )
        self.assertEqual(
            scout.payment_signal(issue(body="", labels=[{"name": "bounty"}, "$40"])),
            "bounty labels: $40",
        )
        self.assertEqual(
            scout.payment_signal(issue(body="Funded through Opire", title="Task")),
            "named bounty platform + funding language",
        )
        self.assertIsNone(scout.payment_signal(issue(body="bounty maybe someday", title="Task")))

    def test_issue_repo_and_number(self):
        self.assertEqual(scout.issue_repo_and_number(issue()), ("acme/widget", 42))
        self.assertEqual(
            scout.issue_repo_and_number({"html_url": "https://example.com/no"}),
            (None, None),
        )

    def test_usd_like_amount(self):
        self.assertEqual(scout.usd_like_amount_from_signal("reward $1,234.50"), 1234.5)
        self.assertEqual(scout.usd_like_amount_from_signal("reward 25 USDC"), 25.0)
        self.assertIsNone(scout.usd_like_amount_from_signal("reward €25"))
        self.assertIsNone(scout.usd_like_amount_from_signal(None))

    def test_parse_datetime(self):
        self.assertIsNotNone(scout.parse_github_datetime("2026-09-30T12:00:00Z"))
        self.assertIsNone(scout.parse_github_datetime(None))
        self.assertIsNone(scout.parse_github_datetime("not-a-date"))


class CompetitionTests(unittest.TestCase):
    def test_existing_pr_detected_and_absent(self):
        prs = {"items": [{
            "title": "Fixes #42",
            "body": "",
            "html_url": "https://github.com/acme/widget/pull/9",
        }]}
        with patch.object(scout, "search_github", return_value=prs):
            self.assertIn("pull/9", scout.has_existing_implementation_pr("acme/widget", 42, "t"))

        with patch.object(scout, "search_github", return_value={"items": [{"title": "Other", "body": ""}]}):
            self.assertIsNone(scout.has_existing_implementation_pr("acme/widget", 42, "t"))

    def test_active_claim_paths(self):
        self.assertIsNone(scout.active_claim_reason("a/b", 1, 0, "t"))
        with patch.object(scout, "github_get", return_value=None):
            self.assertIsNone(scout.active_claim_reason("a/b", 1, 2, "t"))

        comments = [{"body": "I'm working on this", "user": {"login": "dev"}}]
        with patch.object(scout, "github_get", return_value=comments):
            self.assertEqual(
                scout.active_claim_reason("a/b", 1, 2, "t"),
                "active claim by @dev",
            )
        with patch.object(scout, "github_get", return_value=[{"body": "Interesting issue"}]):
            self.assertIsNone(scout.active_claim_reason("a/b", 1, 1, "t"))


class EligibilityTests(unittest.TestCase):
    def test_clean_candidate_rejections_and_acceptance(self):
        cases = [
            issue(pull_request={}),
            issue(html_url="https://github.com/laclance/BountyScout/issues/1"),
            issue(title="Bounty Alert: generated"),
            issue(body="Active bounty scan results"),
            issue(assignees=[{"login": "x"}]),
            issue(comments=scout.MAX_COMMENTS + 1),
            issue(title="Write blog post"),
        ]
        for item in cases:
            with self.subTest(item=item):
                self.assertFalse(scout.is_clean_candidate(item))
        self.assertTrue(scout.is_clean_candidate(issue()))

    def test_candidate_rejection_paths(self):
        proposal = issue(body="[Bounty proposal] $100")
        self.assertEqual(
            scout.candidate_rejection_reason(proposal, "t")[0],
            "unfunded bounty proposal, not an existing award",
        )
        meta = issue(body="bounty-watch $100 reward")
        self.assertEqual(
            scout.candidate_rejection_reason(meta, "t")[0],
            "meta/monitoring alert, not a contributor task",
        )
        unpaid = issue(body="plain bug", title="Bug")
        self.assertEqual(
            scout.candidate_rejection_reason(unpaid, "t")[0],
            "no explicit payment signal",
        )
        bad_url = issue(html_url="not-github")
        reason, signal = scout.candidate_rejection_reason(bad_url, "t")
        self.assertEqual(reason, "could not identify repository/issue number")
        self.assertTrue(signal)

        with patch.object(scout, "has_existing_implementation_pr", return_value="existing pr"):
            self.assertEqual(scout.candidate_rejection_reason(issue(), "t")[0], "existing pr")

        with patch.object(scout, "has_existing_implementation_pr", return_value=None), \
             patch.object(scout, "active_claim_reason", return_value="active claim"):
            self.assertEqual(scout.candidate_rejection_reason(issue(comments=1), "t")[0], "active claim")

        with patch.object(scout, "has_existing_implementation_pr", return_value=None), \
             patch.object(scout, "active_claim_reason", return_value=None):
            reason, signal = scout.candidate_rejection_reason(issue(), "t")
            self.assertIsNone(reason)
            self.assertIn("$100", signal)


class RankingTests(unittest.TestCase):
    def test_fetch_repo_metadata(self):
        with patch.object(scout, "github_get", return_value={"stargazers_count": 10}):
            self.assertEqual(scout.fetch_repo_metadata("a/b", "t")["stargazers_count"], 10)
        with patch.object(scout, "github_get", return_value=[]):
            self.assertEqual(scout.fetch_repo_metadata("a/b", "t"), {})

    def test_score_candidate_signal_reward_and_tier_branches(self):
        now = datetime.now(timezone.utc)
        base_item = issue(created_at=now.isoformat(), comments=0, labels=["help wanted"])

        score, tier, reasons = scout.score_candidate(
            base_item,
            "explicit bounty command: $500",
            {"stargazers_count": 2000, "pushed_at": now.isoformat()},
        )
        self.assertEqual(tier, "strong")
        self.assertIn("explicit bounty command", reasons)

        variants = [
            ("bounty labels: $25", 150, now - timedelta(days=60), 1),
            ("named bounty platform + funding language", 15, now - timedelta(days=200), 4),
            ("payment term + amount: $4", 0, None, 8),
            ("payment term + amount: $10", 0, None, 3),
        ]
        for signal, stars, pushed, comments in variants:
            item = issue(
                title="Epic rewrite" if comments == 8 else "Task",
                body="tests roadmap" if comments == 8 else "",
                comments=comments,
                created_at=(now - timedelta(days=20)).isoformat(),
            )
            meta = {"stargazers_count": stars}
            if pushed:
                meta["pushed_at"] = pushed.isoformat()
            score, tier, reasons = scout.score_candidate(item, signal, meta)
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)
            self.assertIn(tier, {"strong", "promising", "low-confidence"})

        score, tier, reasons = scout.score_candidate(
            issue(title="Task", body="", comments=0, created_at=None),
            "payment term + amount: $1",
            {"stargazers_count": 0, "archived": True},
        )
        self.assertEqual(tier, "low-confidence")
        self.assertIn("archived repo", reasons)


class NotificationTests(unittest.TestCase):
    def _assert_post(self, fn, *args):
        with patch.object(scout.urllib.request, "urlopen", return_value=FakeResponse()) as opened:
            self.assertTrue(fn(*args))
            req = opened.call_args.args[0]
            self.assertEqual(req.method, "POST")
            return req

    def test_notification_success_payloads(self):
        req = self._assert_post(scout.send_telegram_notification, "bot", "chat", "hello")
        self.assertIn(b'"chat_id": "chat"', req.data)

        req = self._assert_post(scout.send_discord_notification, "https://hook", "hello")
        self.assertIn(b'"content": "hello"', req.data)

        req = self._assert_post(
            scout.create_github_issue,
            "me/repo",
            "tok",
            "title",
            "body",
        )
        self.assertIn(b'"bounty-alert"', req.data)
        self.assertEqual(req.headers["Authorization"], "Bearer tok")

    def test_notification_failures(self):
        with patch.object(scout.urllib.request, "urlopen", side_effect=OSError("x")):
            self.assertFalse(scout.send_telegram_notification("bot", "chat", "m"))
            self.assertFalse(scout.send_discord_notification("https://hook", "m"))
            self.assertFalse(scout.create_github_issue("a/b", "t", "x", "y"))


class MainTests(unittest.TestCase):
    def test_main_no_candidates(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(scout, "load_seen_bounties", return_value=set()), \
             patch.object(scout, "search_github", return_value={"items": []}), \
             io.StringIO() as buf, redirect_stdout(buf):
            scout.main()
            self.assertIn("No new clean paid bounty opportunities found.", buf.getvalue())

    def test_main_full_delivery_marks_seen_and_dedupes(self):
        item = issue(comments=1)
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/BountyScout",
            "TELEGRAM_BOT_TOKEN": "tb",
            "TELEGRAM_CHAT_ID": "chat",
            "DISCORD_WEBHOOK_URL": "hook",
        }
        search_results = {"items": [item, item]}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(scout, "load_seen_bounties", return_value=set()), \
             patch.object(scout, "search_github", return_value=search_results), \
             patch.object(scout, "candidate_rejection_reason", return_value=(None, "payment term + amount: $100")), \
             patch.object(scout, "fetch_repo_metadata", return_value={"stargazers_count": 500}), \
             patch.object(scout, "send_telegram_notification", return_value=True) as tg, \
             patch.object(scout, "send_discord_notification", return_value=True) as dc, \
             patch.object(scout, "create_github_issue", return_value=True) as gh, \
             patch.object(scout, "save_seen_bounties", return_value=True) as save:
            scout.main()
            tg.assert_called_once()
            dc.assert_called_once()
            gh.assert_called_once()
            save.assert_called_once_with({item["html_url"]})

    def test_main_rejected_seen_and_failed_delivery_do_not_save(self):
        seen_item = issue(html_url="https://github.com/acme/widget/issues/1")
        rejected_item = issue(html_url="https://github.com/acme/widget/issues/2")
        accepted_item = issue(html_url="https://github.com/acme/widget/issues/3")
        results = {"items": [seen_item, rejected_item, accepted_item]}
        env = {"TELEGRAM_BOT_TOKEN": "tb", "TELEGRAM_CHAT_ID": "chat"}

        def rejection(item_, token):
            if item_["html_url"].endswith("/2"):
                return "no explicit payment signal", None
            return None, "payment term + amount: $25"

        with patch.dict(os.environ, env, clear=True), \
             patch.object(scout, "load_seen_bounties", return_value={seen_item["html_url"]}), \
             patch.object(scout, "search_github", return_value=results), \
             patch.object(scout, "candidate_rejection_reason", side_effect=rejection), \
             patch.object(scout, "fetch_repo_metadata", return_value={}), \
             patch.object(scout, "send_telegram_notification", return_value=False), \
             patch.object(scout, "save_seen_bounties") as save:
            scout.main()
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
