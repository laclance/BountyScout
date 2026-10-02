from __future__ import annotations

import ast
import io
import os
import urllib.request
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from unittest.mock import patch

import scout_bounties as scout
from bountyscout import state


def issue(**overrides: Any) -> dict[str, Any]:
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
    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> Literal[False]:
        return False

    def read(self) -> bytes:
        return self.body


class HttpTests(unittest.TestCase):
    def test_github_get_success_and_failure(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(b'{"ok": true}'),
        ) as opened:
            self.assertEqual(scout.github_get("https://example", "tok", 3), {"ok": True})
            req = opened.call_args.args[0]
            self.assertEqual(req.headers["Authorization"], "Bearer tok")
            self.assertEqual(opened.call_args.kwargs["timeout"], 3)

        with patch.object(urllib.request, "urlopen", side_effect=OSError("boom")):
            self.assertIsNone(scout.github_get("https://example"))

    def test_search_github_encodes_and_normalizes(self) -> None:
        with patch.object(scout, "github_get", return_value={"items": [1]}) as get:
            self.assertEqual(scout.search_github("a b", "t", per_page=7), {"items": [1]})
            self.assertIn("per_page=7", get.call_args.args[0])
            self.assertIn("q=a+b", get.call_args.args[0])
        with patch.object(scout, "github_get", return_value=[]):
            self.assertEqual(scout.search_github("x"), {})


class PaymentTests(unittest.TestCase):
    def test_payment_signal_variants(self) -> None:
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
        self.assertEqual(
            scout.payment_signal(issue(body="20 USDC bounty", title="Task")),
            "amount + payment term: 20 USDC",
        )
        self.assertEqual(
            scout.payment_signal(issue(body="$20 bounty", title="Task")),
            "amount + payment term: $20",
        )
        self.assertIsNone(
            scout.payment_signal(issue(body="Reward address: ERC-20 USDC", title="Task"))
        )
        chain_love_body = (
            "No payout is assumed unless the DBIP is approved through the official "
            "Chain.Love compensation process.\n\n"
            "### Rewards address\n\n"
            "Will provide an Ethereum-mainnet ERC-20 USDC/USDT address upon approval "
            "if required."
        )
        self.assertIsNone(
            scout.payment_signal(issue(body=chain_love_body, title="[DBIP] Proposal"))
        )
        self.assertIsNone(scout.payment_signal(issue(body="bounty maybe someday", title="Task")))

    def test_issue_repo_and_number(self) -> None:
        self.assertEqual(scout.issue_repo_and_number(issue()), ("acme/widget", 42))
        self.assertEqual(
            scout.issue_repo_and_number({"html_url": "https://example.com/no"}),
            (None, None),
        )

    def test_usd_like_amount(self) -> None:
        self.assertEqual(scout.usd_like_amount_from_signal("reward $1,234.50"), 1234.5)
        self.assertEqual(scout.usd_like_amount_from_signal("reward 25 USDC"), 25.0)
        self.assertIsNone(scout.usd_like_amount_from_signal("reward €25"))
        self.assertIsNone(scout.usd_like_amount_from_signal(None))

    def test_parse_datetime(self) -> None:
        self.assertIsNotNone(scout.parse_github_datetime("2026-09-30T12:00:00Z"))
        self.assertIsNone(scout.parse_github_datetime(None))
        self.assertIsNone(scout.parse_github_datetime("not-a-date"))


class CompetitionTests(unittest.TestCase):
    def test_existing_pr_detected_and_absent(self) -> None:
        timeline = [
            {"event": "commented"},
            {"event": "cross-referenced", "source": None},
            {"event": "cross-referenced", "source": {"issue": None}},
            {
                "event": "cross-referenced",
                "source": {"issue": {"state": "open"}},
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "closed",
                        "html_url": "https://github.com/acme/widget/pull/8",
                    }
                },
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "state": "open",
                        "html_url": "https://github.com/acme/widget/pull/9",
                    }
                },
            },
        ]
        with patch.object(scout, "github_get", return_value=timeline):
            reason = scout.has_existing_implementation_pr("acme/widget", 42, "t")
            self.assertIsNotNone(reason)
            assert reason is not None
            self.assertIn("pull/9", reason)

        with patch.object(scout, "github_get", return_value=None):
            self.assertIsNone(scout.has_existing_implementation_pr("acme/widget", 42, "t"))

        with patch.object(
            scout,
            "github_get",
            return_value=[
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "pull_request": {},
                            "state": "open",
                        }
                    },
                }
            ],
        ):
            self.assertIsNone(scout.has_existing_implementation_pr("acme/widget", 42, "t"))

    def test_active_claim_paths(self) -> None:
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
    def test_clean_candidate_rejections_and_acceptance(self) -> None:
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

    def test_candidate_rejection_paths(self) -> None:
        proposal = issue(body="[Bounty proposal] $100")
        self.assertEqual(
            scout.candidate_rejection_reason(proposal, "t")[0],
            "unfunded bounty proposal, not an existing award",
        )
        markdown_proposal = issue(
            body=(
                "**Bounty proposal**\n\n"
                "Would you approve **US$100 cash upon acceptance and merge** "
                "for this focused correction? Please confirm eligibility first."
            )
        )
        self.assertEqual(
            scout.candidate_rejection_reason(markdown_proposal, "t")[0],
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

        with (
            patch.object(scout, "has_existing_implementation_pr", return_value=None),
            patch.object(scout, "active_claim_reason", return_value="active claim"),
        ):
            self.assertEqual(
                scout.candidate_rejection_reason(issue(comments=1), "t")[0], "active claim"
            )

        with (
            patch.object(scout, "has_existing_implementation_pr", return_value=None),
            patch.object(scout, "active_claim_reason", return_value=None),
        ):
            reason, signal = scout.candidate_rejection_reason(issue(), "t")
            self.assertIsNone(reason)
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertIn("$100", signal)


class RankingTests(unittest.TestCase):
    def test_fetch_repo_metadata(self) -> None:
        with patch.object(scout, "github_get", return_value={"stargazers_count": 10}):
            self.assertEqual(scout.fetch_repo_metadata("a/b", "t")["stargazers_count"], 10)
        with patch.object(scout, "github_get", return_value=[]):
            self.assertEqual(scout.fetch_repo_metadata("a/b", "t"), {})

    def test_score_candidate_signal_reward_and_tier_branches(self) -> None:
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
            meta: dict[str, Any] = {"stargazers_count": stars}
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
    def _assert_post(self, fn: Any, *args: Any) -> Any:
        with patch.object(urllib.request, "urlopen", return_value=FakeResponse()) as opened:
            self.assertTrue(fn(*args))
            req = opened.call_args.args[0]
            self.assertEqual(req.method, "POST")
            return req

    def test_notification_success_payloads(self) -> None:
        req = self._assert_post(scout.send_telegram_notification, "bot", "chat", "hello")
        self.assertIn(b'"chat_id": "chat"', req.data)

        req = self._assert_post(scout.send_discord_notification, "https://hook", "hello")
        self.assertIn(b'"content": "hello"', req.data)

    def test_github_issue_report_is_closed_not_planned(self) -> None:
        created = FakeResponse(b'{"url": "https://api.github.com/repos/me/repo/issues/42"}')
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=[created, FakeResponse()],
        ) as opened:
            self.assertTrue(
                scout.create_github_issue(
                    "me/repo",
                    "tok",
                    "title",
                    "body",
                )
            )

        self.assertEqual(opened.call_count, 2)
        create_req = opened.call_args_list[0].args[0]
        close_req = opened.call_args_list[1].args[0]
        self.assertEqual(create_req.method, "POST")
        self.assertIn(b'"bounty-alert"', create_req.data)
        self.assertEqual(create_req.headers["Authorization"], "Bearer tok")
        self.assertEqual(close_req.method, "PATCH")
        self.assertEqual(
            close_req.full_url,
            "https://api.github.com/repos/me/repo/issues/42",
        )
        self.assertIn(b'"state": "closed"', close_req.data)
        self.assertIn(b'"state_reason": "not_planned"', close_req.data)

    def test_github_issue_report_requires_created_issue_url(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(b"{}"),
        ) as opened:
            self.assertFalse(scout.create_github_issue("me/repo", "tok", "title", "body"))
        opened.assert_called_once()

    def test_github_issue_report_rejects_non_object_create_response(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(b"[]"),
        ) as opened:
            self.assertFalse(scout.create_github_issue("me/repo", "tok", "title", "body"))
        opened.assert_called_once()

    def test_github_issue_report_fails_when_auto_close_fails(self) -> None:
        created = FakeResponse(b'{"url": "https://api.github.com/repos/me/repo/issues/42"}')
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=[created, OSError("close failed")],
        ):
            self.assertFalse(scout.create_github_issue("me/repo", "tok", "title", "body"))

    def test_notification_failures(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=OSError("x")):
            self.assertFalse(scout.send_telegram_notification("bot", "chat", "m"))
            self.assertFalse(scout.send_discord_notification("https://hook", "m"))
            self.assertFalse(scout.create_github_issue("a/b", "t", "x", "y"))


class MainTests(unittest.TestCase):
    def test_main_no_candidates(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "search_github", return_value={"items": []}),
            io.StringIO() as buf,
            redirect_stdout(buf),
        ):
            scout.main()
            self.assertIn("No new clean paid bounty opportunities found.", buf.getvalue())

    def test_main_full_delivery_marks_seen_and_dedupes(self) -> None:
        item = issue(comments=1)
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/BountyScout",
            "TELEGRAM_BOT_TOKEN": "tb",
            "TELEGRAM_CHAT_ID": "chat",
            "DISCORD_WEBHOOK_URL": "hook",
        }
        search_results = {"items": [item, item]}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "search_github", return_value=search_results),
            patch.object(
                scout,
                "candidate_rejection_reason",
                return_value=(None, "payment term + amount: $100"),
            ),
            patch.object(scout, "fetch_repo_metadata", return_value={"stargazers_count": 500}),
            patch.object(scout, "send_telegram_notification", return_value=True) as tg,
            patch.object(scout, "send_discord_notification", return_value=True) as dc,
            patch.object(scout, "create_github_issue", return_value=True) as gh,
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()
            tg.assert_called_once()
            dc.assert_called_once()
            gh.assert_called_once()
            save.assert_called_once()
            saved_state = save.call_args.args[0]
            self.assertTrue(saved_state.contains(item["html_url"]))

    def test_main_rejected_seen_and_failed_delivery_do_not_save(self) -> None:
        seen_item = issue(html_url="https://github.com/acme/widget/issues/1")
        rejected_item = issue(html_url="https://github.com/acme/widget/issues/2")
        accepted_item = issue(html_url="https://github.com/acme/widget/issues/3")
        results = {"items": [seen_item, rejected_item, accepted_item]}
        env = {"TELEGRAM_BOT_TOKEN": "tb", "TELEGRAM_CHAT_ID": "chat"}

        def rejection(item_: dict[str, Any], token: str | None) -> tuple[str | None, str | None]:
            if item_["html_url"].endswith("/2"):
                return "no explicit payment signal", None
            return None, "payment term + amount: $25"

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                state,
                "load_seen_state",
                return_value=state.SeenState.from_urls([seen_item["html_url"]]),
            ),
            patch.object(scout, "search_github", return_value=results),
            patch.object(scout, "candidate_rejection_reason", side_effect=rejection),
            patch.object(scout, "fetch_repo_metadata", return_value={}),
            patch.object(scout, "send_telegram_notification", return_value=False),
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()
            save.assert_not_called()

    def test_main_state_load_error_stops_before_discovery(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                state,
                "load_seen_state",
                side_effect=state.SeenStateLoadError("corrupt state"),
            ),
            patch.object(scout, "search_github") as search,
        ):
            with self.assertRaises(state.SeenStateLoadError):
                scout.main()

        search.assert_not_called()

    def test_main_github_delivery_failure_does_not_advance_state(self) -> None:
        item = issue(comments=0)
        env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "me/BountyScout"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "search_github", return_value={"items": [item]}),
            patch.object(
                scout,
                "candidate_rejection_reason",
                return_value=(None, "payment term + amount: $100"),
            ),
            patch.object(scout, "fetch_repo_metadata", return_value={}),
            patch.object(scout, "create_github_issue", return_value=False),
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()

        save.assert_not_called()


class CoverageGapTests(unittest.TestCase):
    def test_score_zero_reward_old_issue_and_no_star_branches(self) -> None:
        now = datetime.now(timezone.utc)
        score, tier, reasons = scout.score_candidate(
            issue(
                title="Task",
                body="",
                comments=6,
                created_at=(now - timedelta(days=120)).isoformat(),
            ),
            "payment term + amount: $0",
            {
                "stargazers_count": 0,
                "pushed_at": (now - timedelta(days=365)).isoformat(),
            },
        )
        self.assertGreaterEqual(score, 0)
        self.assertIn(tier, {"strong", "promising", "low-confidence"})

    def test_main_skips_dirty_reuses_repo_cache_and_can_fail_state_save(self) -> None:
        dirty = issue(
            html_url="https://github.com/acme/widget/issues/40",
            assignees=[{"login": "taken"}],
        )
        first = issue(html_url="https://github.com/acme/widget/issues/41", comments=0)
        second = issue(html_url="https://github.com/acme/widget/issues/42", comments=0)
        env = {"DISCORD_WEBHOOK_URL": "https://hook"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "search_github", return_value={"items": [dirty, first, second]}),
            patch.object(
                scout,
                "candidate_rejection_reason",
                return_value=(None, "payment term + amount: $25"),
            ),
            patch.object(scout, "fetch_repo_metadata", return_value={}) as meta,
            patch.object(scout, "send_discord_notification", return_value=True),
            patch.object(
                state,
                "save_seen_state",
                side_effect=state.SeenStateSaveError("save failed"),
            ) as save,
        ):
            scout.main()
        meta.assert_called_once_with("acme/widget", None)
        save.assert_called_once()

    def test_main_short_circuit_telegram_condition_without_chat(self) -> None:
        item_ = issue(comments=0)
        env = {"TELEGRAM_BOT_TOKEN": "tb", "DISCORD_WEBHOOK_URL": "https://hook"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(state, "load_seen_state", return_value=state.SeenState()),
            patch.object(scout, "search_github", return_value={"items": [item_]}),
            patch.object(
                scout,
                "candidate_rejection_reason",
                return_value=(None, "payment term + amount: $25"),
            ),
            patch.object(scout, "fetch_repo_metadata", return_value={}),
            patch.object(scout, "send_telegram_notification") as tg,
            patch.object(scout, "send_discord_notification", return_value=False),
            patch.object(state, "save_seen_state") as save,
        ):
            scout.main()
        tg.assert_not_called()
        save.assert_not_called()


class TypingCoverageTests(unittest.TestCase):
    def test_every_python_function_is_annotated(self) -> None:
        for path in sorted(Path(".").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                with self.subTest(path=str(path), function=node.name, line=node.lineno):
                    self.assertIsNotNone(node.returns, "missing return annotation")
                    arguments = [
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                    ]
                    for argument in arguments:
                        if argument.arg in {"self", "cls"}:
                            continue
                        self.assertIsNotNone(
                            argument.annotation,
                            f"missing annotation for {argument.arg}",
                        )
                    if node.args.vararg is not None:
                        self.assertIsNotNone(
                            node.args.vararg.annotation,
                            f"missing annotation for *{node.args.vararg.arg}",
                        )
                    if node.args.kwarg is not None:
                        self.assertIsNotNone(
                            node.args.kwarg.annotation,
                            f"missing annotation for **{node.args.kwarg.arg}",
                        )


if __name__ == "__main__":
    unittest.main()
