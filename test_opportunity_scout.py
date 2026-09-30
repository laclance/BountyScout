from __future__ import annotations

import io
import os
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from unittest.mock import patch

import opportunity_scout as scout
import scout_bounties as bounty


def issue(**overrides: Any) -> dict[str, Any]:
    base = {
        "html_url": "https://github.com/example/project/issues/42",
        "state": "open",
        "comments": 1,
        "title": "Fix deterministic network regression",
        "body": "",
        "labels": [],
        "assignees": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


def repo_meta(**overrides: Any) -> dict[str, Any]:
    base = {
        "stargazers_count": 1500,
        "pushed_at": datetime.now(timezone.utc).isoformat(),
        "language": "Go",
        "archived": False,
    }
    base.update(overrides)
    return base


def candidate(**overrides: Any) -> dict[str, Any]:
    base = {
        "repo": "example/project",
        "issue_number": 42,
        "title": "Fix deterministic network regression",
        "url": "https://github.com/example/project/issues/42",
        "paid": True,
        "reward": "$100",
        "payment_confidence": 100,
        "cash_score": 80,
        "career_score": 75,
        "priority_score": 85,
        "effort": "1–3h",
        "expected_hourly": 50.0,
        "competition": "low",
        "stars": 1500,
        "recent_activity": "active in last 7d",
        "language": "Go",
        "labels": ["help wanted"],
        "cash_reasons": ["payment confidence 100/100"],
        "career_reasons": ["target infrastructure/domain fit"],
        "contribution_guide": "https://github.com/example/project/CONTRIBUTING.md",
        "comments": 1,
        "updated_at": "2026-09-30T12:00:00Z",
        "rejection_reason": None,
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


class BasicHeuristicTests(unittest.TestCase):
    def test_target_repo_queries_chunk_all_targets(self) -> None:
        queries = scout.target_repo_queries()
        self.assertEqual(len(queries), 5)
        joined = " ".join(queries)
        for repo in scout.TARGET_REPOS:
            self.assertIn(f"repo:{repo}", joined)

    def test_issue_text_handles_dict_and_string_labels(self) -> None:
        title, body, labels, text = scout.issue_text(
            issue(title="ABC", body="DEF", labels=[{"name": "Help Wanted"}, "Bug"])
        )
        self.assertEqual((title, body), ("ABC", "DEF"))
        self.assertEqual(labels, "help wanted bug")
        self.assertEqual(text, "abc\ndef")

    def test_effort_all_buckets(self) -> None:
        self.assertEqual(scout.code_reference_count("a.go a.go pkg/b.sh docs/c.yaml"), 3)
        self.assertEqual(scout.estimate_effort(issue(title="Architecture rewrite")), "1d+")
        self.assertEqual(scout.estimate_effort(issue(title="FR: Support ExternalName")), "1d+")
        self.assertEqual(
            scout.estimate_effort(
                issue(title="Add support for Connection Pool", labels=["kind/feature"])
            ),
            "1d+",
        )
        self.assertEqual(
            scout.estimate_effort(
                issue(title="Android DNS regression", body="dual SIM device reproduction")
            ),
            "1d+",
        )
        self.assertEqual(scout.estimate_effort(issue(title="README typo", body="small")), "<1h")
        self.assertEqual(
            scout.estimate_effort(
                issue(title="TCP mode leaks upstream connection", comments=0)
            ),
            "1–3h",
        )
        self.assertEqual(
            scout.estimate_effort(
                issue(
                    title="Broad logger cleanup",
                    body="a.go b.go c.go d.go",
                    comments=1,
                )
            ),
            "6–12h",
        )
        self.assertEqual(
            scout.estimate_effort(issue(title="Broad bug", body="x" * 9000, comments=1)),
            "6–12h",
        )
        self.assertEqual(
            scout.estimate_effort(
                issue(
                    title="Android split tunnel bug",
                    body="### Steps to reproduce\n\n_No response_",
                    labels=["OS-android"],
                    comments=8,
                )
            ),
            "6–12h",
        )
        self.assertEqual(
            scout.estimate_effort(
                issue(
                    title="Watcher backlog",
                    body="Suggested fixes\n- first\n- second\n- third",
                    comments=0,
                )
            ),
            "6–12h",
        )
        self.assertEqual(scout.estimate_effort(issue(title="Feature", body="x" * 13000)), "1d+")
        self.assertEqual(scout.estimate_effort(issue(title="Feature", comments=13)), "1d+")
        self.assertEqual(
            scout.estimate_effort(issue(title="Feature", body="normal", comments=4)), "3–6h"
        )

    def test_effort_hours_and_competition(self) -> None:
        self.assertEqual(scout.effort_hours("<1h"), 0.75)
        self.assertEqual(scout.effort_hours("1–3h"), 2.0)
        self.assertEqual(scout.effort_hours("3–6h"), 4.5)
        self.assertEqual(scout.effort_hours("6–12h"), 9.0)
        self.assertEqual(scout.effort_hours("1d+"), 16.0)
        for comments, expected in [(0, "none"), (3, "low"), (8, "medium"), (9, "high")]:
            self.assertEqual(scout.competition(issue(comments=comments)), expected)

    def test_payment_confidence_all_classes(self) -> None:
        cases = [
            (None, 0),
            ("confirmed bounty platform feed (Opire)", 100),
            ("explicit bounty command: $1", 100),
            ("explicit /reward comment: $1", 98),
            ("explicit /bounty comment: $1", 98),
            ("bounty labels: $1", 95),
            ("named bounty platform + funding language", 90),
            ("payment term + amount: $1", 85),
        ]
        for signal, expected in cases:
            self.assertEqual(scout.payment_confidence(signal), expected)

    def test_reward_text_and_repo_activity(self) -> None:
        self.assertIsNone(scout.reward_text(None))
        self.assertEqual(scout.reward_text("reward 25 CAD"), "25 CAD")
        self.assertIsNone(scout.reward_text("funded externally"))

        self.assertEqual(scout.repo_activity({}), "unknown")
        now = datetime.now(timezone.utc)
        for days, prefix in [
            (2, "active in last 7d"),
            (20, "active in last 30d"),
            (60, "active in last 90d"),
            (120, "last push"),
        ]:
            meta = {"pushed_at": (now - timedelta(days=days)).isoformat()}
            self.assertTrue(scout.repo_activity(meta).startswith(prefix))


class HttpAndPlatformTests(unittest.TestCase):
    def test_github_get_optional_success_failure_and_auth(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(b'{"html_url":"x"}'),
        ) as opened:
            self.assertEqual(scout.github_get_optional("https://x", "tok"), {"html_url": "x"})
            self.assertEqual(opened.call_args.args[0].headers["Authorization"], "Bearer tok")
        with patch.object(urllib.request, "urlopen", side_effect=OSError("x")):
            self.assertIsNone(scout.github_get_optional("https://x", None))

    def test_fetch_text_success_and_failure(self) -> None:
        with patch.object(urllib.request, "urlopen", return_value=FakeResponse(b"hello")):
            self.assertEqual(scout.fetch_text("https://x"), "hello")
        with patch.object(urllib.request, "urlopen", side_effect=OSError("x")):
            self.assertEqual(scout.fetch_text("https://x"), "")

    def test_issue_comments_paths(self) -> None:
        self.assertEqual(scout.issue_comments({"html_url": "bad", "comments": 2}, "t"), [])
        self.assertEqual(scout.issue_comments(issue(comments=0), "t"), [])
        with patch.object(bounty, "github_get", return_value={"not": "list"}):
            self.assertEqual(scout.issue_comments(issue(comments=1), "t"), [])
        with patch.object(bounty, "github_get", return_value=[{"body": "x"}]):
            self.assertEqual(scout.issue_comments(issue(comments=1), "t"), [{"body": "x"}])

    def test_supplemental_payment_signals(self) -> None:
        variants = [
            ("Cash prize: $50 for merge", "explicit paid-work wording: $50"),
            ("Pay $60 as a stipend", "explicit paid-work wording: $60"),
            ("Sponsored work €70", "explicit paid-work wording: €70"),
            ("80 CAD funded task", "explicit paid-work wording: 80 CAD"),
        ]
        for body, expected in variants:
            self.assertEqual(scout.supplemental_payment_signal(issue(body=body)), expected)

        self.assertEqual(
            scout.supplemental_payment_signal(issue(body="See https://bountyhub.dev/x — $90")),
            "named bounty platform + amount (BountyHub): $90",
        )
        self.assertIsNone(scout.supplemental_payment_signal(issue(body="maybe paid someday")))

    def test_comment_payment_confirmations_and_commands(self) -> None:
        comment_cases = [
            (
                {
                    "body": "$25 bounty created - algora.io/x",
                    "user": {"login": "bot"},
                    "author_association": "NONE",
                },
                "confirmed bounty platform comment (Algora): $25",
            ),
            (
                {
                    "body": "Opire reward 30 USDC",
                    "user": {"login": "opire-bot"},
                    "author_association": "NONE",
                },
                "confirmed bounty platform comment (Opire): 30 USDC",
            ),
            (
                {
                    "body": "A bounty of $40 has been created - bountyhub.dev/x",
                    "user": {"login": "bot"},
                    "author_association": "NONE",
                },
                "confirmed bounty platform comment (BountyHub): $40",
            ),
        ]
        for comment, expected in comment_cases:
            with patch.object(scout, "issue_comments", return_value=[comment]):
                self.assertEqual(scout.comment_payment_signal(issue(), "t"), expected)

        with patch.object(
            scout,
            "issue_comments",
            return_value=[
                {"body": "/reward 75", "author_association": "OWNER", "user": {"login": "m"}}
            ],
        ):
            self.assertEqual(
                scout.comment_payment_signal(issue(), "t"), "explicit /reward comment: $75"
            )

        with patch.object(
            scout,
            "issue_comments",
            return_value=[
                {"body": "/bounty $88", "author_association": "MEMBER", "user": {"login": "m"}}
            ],
        ):
            self.assertEqual(
                scout.comment_payment_signal(issue(), "t"), "explicit /bounty comment: $88"
            )

        with patch.object(
            scout,
            "issue_comments",
            return_value=[
                {"body": "/reward 999", "author_association": "NONE", "user": {"login": "x"}}
            ],
        ):
            self.assertIsNone(scout.comment_payment_signal(issue(), "t"))

    def test_issue_from_github_url(self) -> None:
        self.assertIsNone(scout.issue_from_github_url("bad", "t"))
        with patch.object(bounty, "github_get", return_value=[]):
            self.assertIsNone(scout.issue_from_github_url("https://github.com/a/b/issues/1", "t"))
        with patch.object(bounty, "github_get", return_value={"state": "open"}) as get:
            self.assertEqual(
                scout.issue_from_github_url("https://github.com/a/b/issues/1", "t"),
                {"state": "open"},
            )
            self.assertIn("/repos/a/b/issues/1", get.call_args.args[0])

    def test_issuehunt_parser_empty_pagination_amount_and_no_amount(self) -> None:
        pages = {
            "https://oss.issuehunt.io/issues": (
                '<a href="/r/apache/superset/issues/3821">x</a><span>$17.00</span>'
            ),
            "https://oss.issuehunt.io/issues?page=2": (
                '<a href="/r/acme/widget/issues/9">x</a><span>funded</span>'
            ),
        }
        with patch.object(scout, "fetch_text", side_effect=pages.get):
            refs = scout.issuehunt_platform_refs()
        self.assertEqual(
            refs["https://github.com/apache/superset/issues/3821"],
            "confirmed bounty platform feed (IssueHunt): $17.00",
        )
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/9"],
            "confirmed bounty platform feed (IssueHunt)",
        )

        with patch.object(scout, "fetch_text", return_value=""):
            self.assertEqual(scout.issuehunt_platform_refs(), {})

    def test_opire_parser_direct_details_missing_and_no_amount(self) -> None:
        pages = {
            "https://app.opire.dev/home": (
                "https:\\/\\/github.com\\/direct\\/repo\\/issues\\/1 "
                '<a href="/issues/A">a</a><a href="/issues/B">b</a>'
            ),
            "https://app.opire.dev/issues/A": "no github source here",
            "https://app.opire.dev/issues/B": "https://github.com/acme/widget/issues/2 funded",
        }
        with patch.object(scout, "fetch_text", side_effect=pages.get):
            refs = scout.opire_platform_refs()
        self.assertIn("https://github.com/direct/repo/issues/1", refs)
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/2"],
            "confirmed bounty platform feed (Opire)",
        )
        with patch.object(scout, "fetch_text", return_value=""):
            self.assertEqual(scout.opire_platform_refs(), {})

    def test_bountyhub_parser_direct_details_missing_and_amount(self) -> None:
        pages = {
            "https://www.bountyhub.dev/en/bounties": (
                "https:\\/\\/github.com\\/direct\\/repo\\/issues\\/1 "
                '<a href="/en/bounty/view/A">a</a><a href="/en/bounty/view/B">b</a>'
            ),
            "https://www.bountyhub.dev/en/bounty/view/A": "no github",
            "https://www.bountyhub.dev/en/bounty/view/B": "Reward $125 https://github.com/acme/widget/issues/2",
        }
        with patch.object(scout, "fetch_text", side_effect=pages.get):
            refs = scout.bountyhub_platform_refs()
        self.assertIn("https://github.com/direct/repo/issues/1", refs)
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/2"],
            "confirmed bounty platform feed (BountyHub): $125",
        )
        with patch.object(scout, "fetch_text", return_value=""):
            self.assertEqual(scout.bountyhub_platform_refs(), {})

    def test_platform_paid_refs_merge_precedence(self) -> None:
        with (
            patch.object(scout, "issuehunt_platform_refs", return_value={"u": "issuehunt"}),
            patch.object(scout, "opire_platform_refs", return_value={"v": "opire"}),
            patch.object(scout, "bountyhub_platform_refs", return_value={"u": "bountyhub"}),
        ):
            self.assertEqual(scout.platform_paid_refs(), {"u": "bountyhub", "v": "opire"})

    def test_contribution_guide_found_and_missing(self) -> None:
        def getter(url: str, token: str | None) -> Any:
            return {"html_url": "guide"} if "docs/CONTRIBUTING.md" in url else None

        with patch.object(scout, "github_get_optional", side_effect=getter):
            self.assertEqual(scout.contribution_guide("a/b", "t"), "guide")
        with patch.object(scout, "github_get_optional", return_value=None):
            self.assertIsNone(scout.contribution_guide("a/b", "t"))


class CalibrationTests(unittest.TestCase):
    def test_linked_open_pr_reason_paths(self) -> None:
        self.assertIsNone(
            scout.linked_open_pr_reason(
                {"html_url": "bad", "comments": 1},
                "t",
            )
        )
        self.assertIsNone(scout.linked_open_pr_reason(issue(comments=0), "t"))

        direct_comments = [
            {
                "body": "This is the related PR: https://github.com/example/project/pull/142565",
            }
        ]
        with (
            patch.object(scout, "issue_comments", return_value=direct_comments),
            patch.object(
                bounty,
                "github_get",
                return_value={"state": "open"},
            ),
        ):
            self.assertEqual(
                scout.linked_open_pr_reason(issue(comments=1), "t"),
                "existing open implementation PR: https://github.com/example/project/pull/142565",
            )

        phrase_comments = [
            {"body": "submitted PR #10"},
            {"body": "implementation pull request #11"},
        ]
        with patch.object(
            bounty,
            "github_get",
            side_effect=[
                {"state": "closed", "html_url": "https://github.com/example/project/pull/10"},
                {"state": "open", "html_url": "https://github.com/example/project/pull/11"},
            ],
        ):
            self.assertEqual(
                scout.linked_open_pr_reason(issue(comments=2), "t", phrase_comments),
                "existing open implementation PR: https://github.com/example/project/pull/11",
            )

        with patch.object(bounty, "github_get", return_value=[]):
            self.assertIsNone(
                scout.linked_open_pr_reason(
                    issue(comments=1),
                    "t",
                    [{"body": "opened PR #12"}],
                )
            )

    def test_supplemental_claim_reason_paths(self) -> None:
        self.assertIsNone(scout.supplemental_claim_reason(issue(comments=0), "t"))

        with patch.object(
            scout,
            "issue_comments",
            return_value=[{"body": "Planning a fix:"}],
        ):
            self.assertEqual(
                scout.supplemental_claim_reason(issue(comments=1), "t"),
                "active claim by @someone",
            )

        self.assertEqual(
            scout.supplemental_claim_reason(
                issue(comments=1),
                "t",
                [
                    {
                        "body": "I'll implement this",
                        "user": {"login": "dev"},
                    }
                ],
            ),
            "active claim by @dev",
        )
        self.assertIsNone(
            scout.supplemental_claim_reason(
                issue(comments=1),
                "t",
                [{"body": "Thanks for the report"}],
            )
        )

    def test_extended_competition_reason_all_sources(self) -> None:
        self.assertEqual(
            scout.extended_competition_reason(
                {"html_url": "bad", "comments": 0},
                "t",
            ),
            "could not identify repository/issue number",
        )

        with patch.object(
            bounty,
            "has_existing_implementation_pr",
            return_value="search pr",
        ):
            self.assertEqual(
                scout.extended_competition_reason(issue(), "t"),
                "search pr",
            )

        with (
            patch.object(
                bounty,
                "has_existing_implementation_pr",
                return_value=None,
            ),
            patch.object(
                scout,
                "issue_comments",
                return_value=[{"body": "related PR #8"}],
            ),
            patch.object(
                scout,
                "linked_open_pr_reason",
                return_value="linked pr",
            ),
        ):
            self.assertEqual(
                scout.extended_competition_reason(issue(), "t"),
                "linked pr",
            )

        claim_comments = [
            {
                "body": "I'm working on this",
                "user": {"login": "dev"},
            }
        ]
        with (
            patch.object(
                bounty,
                "has_existing_implementation_pr",
                return_value=None,
            ),
            patch.object(
                scout,
                "issue_comments",
                return_value=claim_comments,
            ),
            patch.object(
                scout,
                "linked_open_pr_reason",
                return_value=None,
            ),
        ):
            self.assertEqual(
                scout.extended_competition_reason(issue(), "t"),
                "active claim by @dev",
            )

        with (
            patch.object(
                bounty,
                "has_existing_implementation_pr",
                return_value=None,
            ),
            patch.object(
                scout,
                "issue_comments",
                return_value=[{"body": "Planning a fix", "user": {"login": "dev2"}}],
            ),
            patch.object(
                scout,
                "linked_open_pr_reason",
                return_value=None,
            ),
        ):
            self.assertEqual(
                scout.extended_competition_reason(issue(), "t"),
                "active claim by @dev2",
            )

        with (
            patch.object(
                bounty,
                "has_existing_implementation_pr",
                return_value=None,
            ),
            patch.object(
                scout,
                "issue_comments",
                return_value=[],
            ),
            patch.object(
                scout,
                "linked_open_pr_reason",
                return_value=None,
            ),
        ):
            self.assertIsNone(scout.extended_competition_reason(issue(), "t"))

    def test_real_queue_calibration_examples(self) -> None:
        aws = issue(
            title="ipamd can allocate an EC2-unassigned IP after restart when IMDS is stale",
            body="deterministic regression",
            labels=[{"name": "bug"}, {"name": "good first issue"}],
            comments=1,
        )
        self.assertEqual(scout.estimate_effort(aws), "1–3h")
        with (
            patch.object(
                scout,
                "issue_comments",
                return_value=[
                    {
                        "body": "Planning a fix:",
                        "user": {"login": "laclance"},
                    }
                ],
            ),
            patch.object(
                bounty,
                "has_existing_implementation_pr",
                return_value=None,
            ),
            patch.object(
                scout,
                "linked_open_pr_reason",
                return_value=None,
            ),
        ):
            self.assertEqual(
                scout.extended_competition_reason(aws, "t"),
                "active claim by @laclance",
            )

        connection_pool = issue(
            title="Client-go: Add support for Connection Pool",
            body="I would like to propose adding support.",
            labels=[{"name": "kind/feature"}, {"name": "needs-triage"}],
        )
        self.assertEqual(scout.estimate_effort(connection_pool), "1d+")
        self.assertEqual(
            scout.strategic_rejection(connection_pool, "t"),
            "awaiting maintainer triage",
        )

        tailscale = issue(
            title="Android DNS regression",
            body="dual SIM + Wi-Fi reproduction on a physical phone",
        )
        self.assertEqual(scout.estimate_effort(tailscale), "1d+")


class CandidateTests(unittest.TestCase):
    def test_build_paid_candidate_scoring(self) -> None:
        item_ = issue(
            body="network concurrency regression tests",
            labels=[{"name": "help wanted"}],
            comments=0,
        )
        result = scout.build_candidate(
            item_,
            "paid",
            "confirmed bounty platform feed (Opire): $500",
            repo_meta(language="Go", stargazers_count=12000),
            "guide",
        )
        self.assertTrue(result["paid"])
        self.assertEqual(result["reward"], "$500")
        self.assertGreater(result["cash_score"], 0)
        self.assertGreater(result["career_score"], 0)
        self.assertEqual(result["competition"], "none")
        self.assertEqual(result["contribution_guide"], "guide")


    def test_issue_specific_ranking_breaks_repo_score_ties(self) -> None:
        meta = repo_meta(language="Go", stargazers_count=37000)
        quick = scout.build_candidate(
            issue(
                html_url="https://github.com/tailscale/tailscale/issues/21590",
                title="cmd/tsnet-proxy: TCP mode leaks upstream connection",
                body="proxyTCP never closes the upstream connection.",
                comments=0,
            ),
            "strategic",
            None,
            meta,
            "guide",
        )
        broad = scout.build_candidate(
            issue(
                html_url="https://github.com/tailscale/tailscale/issues/20958",
                title="containerboot watcher backlog",
                body=(
                    "Cause (from source): watcher queue blocks.\n"
                    "cmd/containerboot/main.go kube/services/services.go\n"
                    "Suggested fixes\n- move refresh\n- resubscribe\n- skip no-op"
                ),
                comments=0,
            ),
            "strategic",
            None,
            meta,
            "guide",
        )
        accepted = scout.build_candidate(
            issue(
                html_url="https://github.com/kubernetes/kubernetes/issues/142384",
                title="gsutil will no longer be available",
                body="get-kube.sh log-dump/log-dump.sh gce/util.sh gce/gci/mounter/stage-upload.sh",
                labels=[{"name": "help wanted"}, {"name": "triage/accepted"}],
                comments=7,
            ),
            "strategic",
            None,
            meta,
            "guide",
        )

        self.assertEqual(quick["effort"], "1–3h")
        self.assertEqual(broad["effort"], "6–12h")
        self.assertEqual(accepted["effort"], "6–12h")
        self.assertGreaterEqual(len({quick["career_score"], broad["career_score"], accepted["career_score"]}), 2)
        self.assertIn("maintainer-ready signal", accepted["career_reasons"])

    def test_build_strategic_candidate_and_non_usd_paid(self) -> None:
        strategic = scout.build_candidate(
            issue(title="Feature", body="api", comments=9),
            "strategic",
            None,
            repo_meta(language="PHP", stargazers_count=15, pushed_at=None),
            None,
        )
        self.assertFalse(strategic["paid"])
        self.assertEqual(strategic["cash_score"], 0)
        self.assertEqual(strategic["competition"], "high")

        paid = scout.build_candidate(
            issue(title="Feature", body="", comments=4),
            "paid",
            "confirmed bounty platform feed (X): €25",
            repo_meta(language="Unknown", stargazers_count=150),
            None,
        )
        self.assertIsNone(paid["expected_hourly"])
        self.assertIn("reward not USD-comparable", paid["cash_reasons"])

    def test_build_candidate_language_effort_competition_branches(self) -> None:
        languages = ["TypeScript", "JavaScript", "Ruby", "HCL", "Rust"]
        for lang in languages:
            result = scout.build_candidate(
                issue(
                    title="README typo" if lang == "Rust" else "Feature",
                    body="storage protocol",
                    comments=2,
                ),
                "strategic",
                None,
                repo_meta(language=lang, stargazers_count=500),
                None,
            )
            self.assertEqual(result["language"], lang)


class VerificationTests(unittest.TestCase):
    def test_refresh_issue_all_paths(self) -> None:
        self.assertEqual(
            scout.refresh_issue({"html_url": "bad"}, "t")[1],
            "could not identify repository/issue number",
        )
        for value, expected in [
            (None, "could not refresh source issue"),
            ({"state": "closed"}, "issue is no longer open"),
            ({"state": "open", "pull_request": {}}, "source is a pull request, not an issue"),
        ]:
            with patch.object(bounty, "github_get", return_value=value):
                self.assertEqual(scout.refresh_issue(issue(), "t")[1], expected)
        with patch.object(bounty, "github_get", return_value=issue()):
            fresh, reason = scout.refresh_issue(issue(), "t")
            self.assertIsNone(reason)
            self.assertIsNotNone(fresh)
            assert fresh is not None
            self.assertEqual(fresh["state"], "open")

    def test_strategic_rejection_paths(self) -> None:
        with patch.object(bounty, "is_clean_candidate", return_value=False):
            self.assertEqual(
                scout.strategic_rejection(issue(), "t"), "failed basic eligibility filter"
            )

        self.assertEqual(
            scout.strategic_rejection(issue(body="OSS Opportunity Queue"), "t"),
            "generated opportunity-scout report",
        )
        self.assertEqual(
            scout.strategic_rejection(issue(labels=["needs-info"]), "t"),
            "support/triage issue rather than a contributor task",
        )
        self.assertEqual(
            scout.strategic_rejection(
                {"html_url": "bad", "title": "x", "body": "", "labels": []}, "t"
            ),
            "could not identify repository/issue number",
        )
        with patch.object(scout, "extended_competition_reason", return_value="pr"):
            self.assertEqual(scout.strategic_rejection(issue(), "t"), "pr")
        with patch.object(scout, "extended_competition_reason", return_value="claim"):
            self.assertEqual(scout.strategic_rejection(issue(), "t"), "claim")

        self.assertEqual(
            scout.strategic_rejection(issue(labels=["needs-triage"]), "t"),
            "awaiting maintainer triage",
        )
        with patch.object(scout, "extended_competition_reason", return_value=None):
            self.assertIsNone(
                scout.strategic_rejection(
                    issue(labels=[{"name": "needs-triage"}, {"name": "good first issue"}]),
                    "t",
                )
            )

    def test_verify_refresh_and_clean_failures(self) -> None:
        with patch.object(scout, "refresh_issue", return_value=(None, "closed")):
            self.assertEqual(scout.verify(issue(), "t", {}, {})[1], "closed")
        with patch.object(scout, "refresh_issue", return_value=(None, None)):
            self.assertEqual(
                scout.verify(issue(), "t", {}, {})[1],
                "could not refresh source issue",
            )
        with (
            patch.object(scout, "refresh_issue", return_value=(issue(), None)),
            patch.object(bounty, "is_clean_candidate", return_value=False),
        ):
            self.assertEqual(
                scout.verify(issue(), "t", {}, {})[1],
                "failed basic eligibility filter after source refresh",
            )

        bad_repo = issue(html_url="not-github", comments=0)
        with (
            patch.object(scout, "refresh_issue", return_value=(bad_repo, None)),
            patch.object(bounty, "is_clean_candidate", return_value=True),
            patch.object(bounty, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(scout, "strategic_rejection", return_value=None),
        ):
            self.assertEqual(
                scout.verify(bad_repo, "t", {}, {})[1],
                "could not identify repository/issue number",
            )

    def test_verify_paid_issue_signal_success_and_repo_failures(self) -> None:
        fresh = issue(body="bounty $100", comments=0)
        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(
                bounty,
                "candidate_rejection_reason",
                return_value=(None, "payment term + amount: $100"),
            ),
            patch.object(bounty, "fetch_repo_metadata", return_value={}),
        ):
            self.assertEqual(
                scout.verify(fresh, "t", {}, {}, True)[1], "repository metadata unavailable"
            )

        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(
                bounty,
                "candidate_rejection_reason",
                return_value=(None, "payment term + amount: $100"),
            ),
            patch.object(bounty, "fetch_repo_metadata", return_value=repo_meta(archived=True)),
        ):
            self.assertEqual(scout.verify(fresh, "t", {}, {}, True)[1], "repository is archived")

        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(
                bounty,
                "candidate_rejection_reason",
                return_value=(None, "payment term + amount: $100"),
            ),
            patch.object(bounty, "fetch_repo_metadata", return_value=repo_meta()),
            patch.object(scout, "contribution_guide", return_value="guide"),
            patch.object(scout, "build_candidate", return_value={"ok": True}),
        ):
            self.assertEqual(scout.verify(fresh, "t", {}, {}, True), ({"ok": True}, None))

    def test_verify_comment_or_override_runs_competition_checks(self) -> None:
        fresh = issue(body="", title="Task", comments=1)
        base = [
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(bounty, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(
                bounty,
                "candidate_rejection_reason",
                return_value=("no explicit payment signal", None),
            ),
        ]
        for p in base:
            p.start()
            self.addCleanup(p.stop)

        with (
            patch.object(
                scout, "comment_payment_signal", return_value="explicit /reward comment: $50"
            ),
            patch.object(scout, "extended_competition_reason", return_value="pr"),
        ):
            self.assertEqual(scout.verify(fresh, "t", {}, {}, True)[1], "pr")

        with (
            patch.object(
                scout, "comment_payment_signal", return_value="explicit /reward comment: $50"
            ),
            patch.object(scout, "extended_competition_reason", return_value="claim"),
        ):
            self.assertEqual(scout.verify(fresh, "t", {}, {}, True)[1], "claim")

        with patch.object(scout, "comment_payment_signal", return_value=None):
            self.assertEqual(
                scout.verify(fresh, "t", {}, {}, True)[1], "no explicit payment signal"
            )

    def test_verify_non_payment_rejection_and_strategic_success(self) -> None:
        paid = issue(body="bounty $100", comments=0)
        with (
            patch.object(scout, "refresh_issue", return_value=(paid, None)),
            patch.object(bounty, "candidate_rejection_reason", return_value=("unfunded", "x")),
        ):
            self.assertEqual(scout.verify(paid, "t", {}, {}, True)[1], "unfunded")

        strategic = issue(body="", title="Feature", comments=0)
        with (
            patch.object(scout, "refresh_issue", return_value=(strategic, None)),
            patch.object(bounty, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(scout, "strategic_rejection", return_value=None),
            patch.object(bounty, "fetch_repo_metadata", return_value=repo_meta()),
            patch.object(scout, "contribution_guide", return_value=None),
            patch.object(scout, "build_candidate", return_value={"lane": "strategic"}),
        ):
            self.assertEqual(
                scout.verify(strategic, "t", {}, {}),
                ({"lane": "strategic"}, None),
            )

    def test_add_reject_caps_examples(self) -> None:
        counts: dict[str, int] = {}
        examples: list[dict[str, Any]] = []
        for i in range(15):
            scout.add_reject(counts, examples, {"html_url": str(i), "title": str(i)}, "why")
        self.assertEqual(counts["why"], 15)
        self.assertEqual(len(examples), 12)


class DiscoveryTests(unittest.TestCase):
    def test_discover_paid_search_and_platform_paths(self) -> None:
        a = issue(html_url="https://github.com/a/a/issues/1")
        duplicate = dict(a)
        dirty = issue(html_url="https://github.com/a/a/issues/2")
        platform = issue(html_url="https://github.com/p/p/issues/3")
        platform_dirty = issue(html_url="https://github.com/p/p/issues/4")
        platform_bad = "https://github.com/p/p/issues/5"

        with (
            patch.object(bounty, "search_github", return_value={"items": [a, duplicate, dirty]}),
            patch.object(
                bounty,
                "is_clean_candidate",
                side_effect=lambda x: (
                    x["html_url"] != dirty["html_url"]
                    and x["html_url"] != platform_dirty["html_url"]
                ),
            ),
            patch.object(
                scout,
                "verify",
                side_effect=[
                    (candidate(url=a["html_url"]), None),
                    (candidate(url=platform["html_url"]), None),
                ],
            ),
            patch.object(
                scout,
                "platform_paid_refs",
                return_value={
                    platform["html_url"]: "sig",
                    platform_dirty["html_url"]: "sig",
                    platform_bad: "sig",
                },
            ),
            patch.object(
                scout,
                "issue_from_github_url",
                side_effect=lambda url, token: {
                    platform["html_url"]: platform,
                    platform_dirty["html_url"]: platform_dirty,
                    platform_bad: None,
                }[url],
            ),
        ):
            found, rejected, examples = scout.discover_paid("t", set(), {}, {})
        selfEqual = self.assertEqual
        selfEqual({x["url"] for x in found}, {a["html_url"], platform["html_url"]})
        selfEqual(rejected, {})
        selfEqual(examples, [])

    def test_discover_paid_records_rejections_and_seen(self) -> None:
        seen = issue(html_url="https://github.com/a/a/issues/1")
        bad = issue(html_url="https://github.com/a/a/issues/2")
        with (
            patch.object(bounty, "search_github", return_value={"items": [seen, bad]}),
            patch.object(bounty, "is_clean_candidate", return_value=True),
            patch.object(scout, "verify", return_value=(None, "claimed")),
            patch.object(scout, "platform_paid_refs", return_value={}),
        ):
            found, rejected, examples = scout.discover_paid("t", {seen["html_url"]}, {}, {})
        self.assertEqual(found, [])
        self.assertGreater(rejected["claimed"], 0)
        self.assertEqual(examples[0]["reason"], "claimed")

    def test_discover_strategic_filters_previews_and_verifies(self) -> None:
        invalid = issue(html_url="bad")
        archived = issue(html_url="https://github.com/x/y/issues/2")
        good = issue(html_url="https://github.com/g/g/issues/3", title="Feature")
        paid = issue(html_url="https://github.com/p/p/issues/4", body="bounty $100")
        items = [invalid, archived, good, paid]

        def meta(repo: str, token: str | None) -> dict[str, Any]:
            if repo == "x/y":
                return repo_meta(archived=True)
            return repo_meta()

        with (
            patch.object(scout, "target_repo_queries", return_value=["q"]),
            patch.object(scout, "STRATEGIC_GLOBAL_QUERIES", []),
            patch.object(bounty, "search_github", return_value={"items": items}),
            patch.object(bounty, "is_clean_candidate", return_value=True),
            patch.object(bounty, "fetch_repo_metadata", side_effect=meta),
            patch.object(
                scout,
                "build_candidate",
                side_effect=lambda item_, lane, signal, meta_, guide: candidate(
                    url=item_["html_url"],
                    paid=(lane == "paid"),
                    priority_score=90 if item_ is paid else 80,
                ),
            ),
            patch.object(
                scout,
                "verify",
                side_effect=lambda item_, *args, **kwargs: (
                    (None, "reject") if item_ is paid else (candidate(url=item_["html_url"]), None)
                ),
            ),
        ):
            found, rejected, examples = scout.discover_strategic("t", set(), set(), {}, {})
        self.assertEqual([x["url"] for x in found], [good["html_url"]])
        self.assertEqual(rejected["reject"], 1)
        self.assertEqual(examples[0]["url"], paid["html_url"])


class FormattingAndMainTests(unittest.TestCase):
    def test_markdown_and_notification_formatting(self) -> None:
        self.assertEqual(
            scout.github_report_ref(
                "https://github.com/acme/widget/issues/42 and "
                "https://github.com/acme/widget/pull/9 plus #77"
            ),
            "https://redirect.github.com/acme/widget/issues/42 and "
            "https://redirect.github.com/acme/widget/pull/9 plus #77",
        )
        self.assertEqual(scout.github_report_ref(None), "")

        md = scout.markdown_candidate(
            candidate(title="Fix regression after #850"),
            1,
        )
        self.assertIn("Cash score", md)
        self.assertNotIn("Career score", md)
        self.assertNotIn("Paid / unpaid", md)
        self.assertNotIn("Rejection reason", md)
        self.assertIn("contribution guide", md)
        self.assertNotIn("https://github.com/example/project/issues/42", md)
        self.assertIn(
            "https://redirect.github.com/example/project/issues/42",
            md,
        )
        self.assertIn("#850", md)

        no_guide = scout.markdown_candidate(
            candidate(expected_hourly=None, contribution_guide=None, paid=False, reward=None),
            2,
        )
        self.assertIn("Career score", no_guide)
        self.assertNotIn("Cash score", no_guide)
        self.assertNotIn("Reward", no_guide)
        self.assertNotIn("Payment confidence", no_guide)
        self.assertNotIn("Expected hourly value", no_guide)
        self.assertNotIn("Rejection reason", no_guide)
        self.assertIn("not found at common paths", no_guide)

        long = candidate(title="x" * 150)
        lines = scout.notification_candidate(long, 1)
        self.assertLessEqual(len(lines[0]), 160)
        self.assertIn("paid bounty", "\n".join(lines))
        self.assertNotIn("career:", "\n".join(lines))

        strategic_lines = scout.notification_candidate(candidate(paid=False), 2)
        self.assertIn("strategic OSS", "\n".join(strategic_lines))
        self.assertIn("career:", "\n".join(strategic_lines))
        self.assertNotIn("reward:", "\n".join(strategic_lines))
        self.assertNotIn("cash:", "\n".join(strategic_lines))

    def test_main_no_queue(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(bounty, "load_seen_bounties", return_value=set()),
            patch.object(scout, "discover_paid", return_value=([], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [])),
            io.StringIO() as buf,
            redirect_stdout(buf),
        ):
            scout.main()
            self.assertIn("No new verified OSS opportunities found.", buf.getvalue())

    def test_main_dedupes_notifies_reports_and_saves(self) -> None:
        low = candidate(priority_score=50, cash_score=50, career_score=50)
        high = candidate(priority_score=90, cash_score=90, career_score=90)
        strategic = candidate(
            url="https://github.com/example/project/issues/43",
            issue_number=43,
            paid=False,
            reward=None,
            priority_score=70,
            expected_hourly=None,
        )
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "me/BountyScout",
            "TELEGRAM_BOT_TOKEN": "tb",
            "TELEGRAM_CHAT_ID": "chat",
            "DISCORD_WEBHOOK_URL": "hook",
        }
        reject = {
            "title": "Related to #123",
            "url": "https://github.com/acme/upstream/issues/123",
            "reason": (
                "existing open implementation PR: https://github.com/acme/upstream/pull/456"
            ),
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(bounty, "load_seen_bounties", return_value={"old"}),
            patch.object(scout, "discover_paid", return_value=([low, high], {"r1": 1}, [reject])),
            patch.object(scout, "discover_strategic", return_value=([strategic], {"r2": 2}, [])),
            patch.object(bounty, "send_telegram_notification", return_value=True) as tg,
            patch.object(bounty, "send_discord_notification", return_value=False) as dc,
            patch.object(bounty, "create_github_issue", return_value=True) as gh,
            patch.object(bounty, "save_seen_bounties", return_value=True) as save,
        ):
            scout.main()
        tg.assert_called_once()
        dc.assert_called_once()
        gh.assert_called_once()
        github_body = gh.call_args.args[3]
        self.assertNotIn("https://github.com/example/project/issues/", github_body)
        self.assertNotIn("https://github.com/acme/upstream/issues/", github_body)
        self.assertNotIn("https://github.com/acme/upstream/pull/", github_body)
        self.assertIn(
            "https://redirect.github.com/example/project/issues/42",
            github_body,
        )
        self.assertIn(
            "https://redirect.github.com/acme/upstream/issues/123",
            github_body,
        )
        self.assertIn(
            "https://redirect.github.com/acme/upstream/pull/456",
            github_body,
        )

        telegram_message = tg.call_args.args[2]
        self.assertIn(high["url"], telegram_message)

        saved = save.call_args.args[0]
        self.assertIn("old", saved)
        self.assertIn(high["url"], saved)
        self.assertIn(strategic["url"], saved)

    def test_main_no_delivery_does_not_save(self) -> None:
        paid = candidate()
        env = {"TELEGRAM_BOT_TOKEN": "tb", "TELEGRAM_CHAT_ID": "chat"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(bounty, "load_seen_bounties", return_value=set()),
            patch.object(scout, "discover_paid", return_value=([paid], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [])),
            patch.object(bounty, "send_telegram_notification", return_value=False),
            patch.object(bounty, "save_seen_bounties") as save,
        ):
            scout.main()
            save.assert_not_called()


class CoverageGapTests(unittest.TestCase):
    def test_trusted_non_command_comment_falls_through_to_next_comment(self) -> None:
        comments = [
            {
                "body": "I support funding this",
                "author_association": "OWNER",
                "user": {"login": "owner"},
            },
            {
                "body": "/reward 7",
                "author_association": "MEMBER",
                "user": {"login": "member"},
            },
        ]
        with patch.object(scout, "issue_comments", return_value=comments):
            self.assertEqual(
                scout.comment_payment_signal(issue(), "t"),
                "explicit /reward comment: $7",
            )

    def test_paid_candidate_under_100_stars(self) -> None:
        result = scout.build_candidate(
            issue(title="Feature", body="plain", comments=4),
            "paid",
            "payment term + amount: $25",
            repo_meta(
                stargazers_count=50,
                pushed_at=(datetime.now(timezone.utc) - timedelta(days=120)).isoformat(),
                language="Rust",
            ),
            None,
        )
        self.assertTrue(result["paid"])
        self.assertEqual(result["stars"], 50)
        self.assertGreater(result["cash_score"], 0)

    def test_platform_detail_empty_amount_and_no_amount_branches(self) -> None:
        opire_pages = {
            "https://app.opire.dev/home": (
                '<a href="/issues/A">a</a><a href="/issues/B">b</a><a href="/issues/C">c</a>'
            ),
            "https://app.opire.dev/issues/A": "",
            "https://app.opire.dev/issues/B": (
                "$50 bounty https://github.com/acme/widget/issues/2"
            ),
            "https://app.opire.dev/issues/C": ("funded https://github.com/acme/widget/issues/3"),
        }
        with patch.object(scout, "fetch_text", side_effect=opire_pages.get):
            refs = scout.opire_platform_refs()
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/2"],
            "confirmed bounty platform feed (Opire): $50",
        )
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/3"],
            "confirmed bounty platform feed (Opire)",
        )

        bountyhub_pages = {
            "https://www.bountyhub.dev/en/bounties": (
                '<a href="/en/bounty/view/A">a</a>'
                '<a href="/en/bounty/view/B">b</a>'
                '<a href="/en/bounty/view/C">c</a>'
            ),
            "https://www.bountyhub.dev/en/bounty/view/A": "",
            "https://www.bountyhub.dev/en/bounty/view/B": (
                "$75 https://github.com/acme/widget/issues/4"
            ),
            "https://www.bountyhub.dev/en/bounty/view/C": (
                "funded https://github.com/acme/widget/issues/5"
            ),
        }
        with patch.object(scout, "fetch_text", side_effect=bountyhub_pages.get):
            refs = scout.bountyhub_platform_refs()
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/4"],
            "confirmed bounty platform feed (BountyHub): $75",
        )
        self.assertEqual(
            refs["https://github.com/acme/widget/issues/5"],
            "confirmed bounty platform feed (BountyHub)",
        )

    def test_build_candidate_remaining_star_activity_target_and_effort_branches(self) -> None:
        inactive = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        cases = [
            (
                issue(title="Feature", body="plain", comments=4),
                "paid",
                "payment term + amount: $25",
                repo_meta(stargazers_count=500, pushed_at=inactive, language="Rust"),
            ),
            (
                issue(title="Feature", body="plain", comments=4),
                "strategic",
                None,
                repo_meta(stargazers_count=1500, pushed_at=inactive, language="Rust"),
            ),
            (
                issue(title="Feature", body="plain", comments=4),
                "strategic",
                None,
                repo_meta(stargazers_count=150, pushed_at=inactive, language="Rust"),
            ),
            (
                issue(title="Feature", body="plain", comments=4),
                "strategic",
                None,
                repo_meta(stargazers_count=0, pushed_at=inactive, language="Rust"),
            ),
            (
                issue(
                    html_url="https://github.com/tailscale/tailscale/issues/99",
                    title="Feature",
                    body="plain",
                    comments=4,
                ),
                "strategic",
                None,
                repo_meta(stargazers_count=50, pushed_at=inactive, language="Go"),
            ),
            (
                issue(title="Architecture redesign", body="plain", comments=0),
                "strategic",
                None,
                repo_meta(stargazers_count=50, pushed_at=inactive, language="Rust"),
            ),
        ]
        results = [
            scout.build_candidate(item_, lane, signal, meta, None)
            for item_, lane, signal, meta in cases
        ]
        self.assertEqual(results[0]["cash_score"] > 0, True)
        self.assertEqual(results[1]["stars"], 1500)
        self.assertEqual(results[3]["stars"], 0)
        self.assertIn("target repo bonus", results[4]["career_reasons"])
        self.assertIn("large-scope penalty", results[5]["career_reasons"])

    def test_verify_success_with_comment_signal_cached_repo_and_guide(self) -> None:
        fresh = issue(body="", title="Task", comments=1)
        repo_cache: dict[str, dict[str, Any]] = {"example/project": repo_meta()}
        guide_cache: dict[str, str | None] = {"example/project": "cached-guide"}
        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(bounty, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(
                scout, "comment_payment_signal", return_value="explicit /reward comment: $50"
            ),
            patch.object(
                bounty,
                "candidate_rejection_reason",
                return_value=("no explicit payment signal", None),
            ),
            patch.object(bounty, "has_existing_implementation_pr", return_value=None),
            patch.object(bounty, "active_claim_reason", return_value=None),
            patch.object(bounty, "fetch_repo_metadata") as fetch_meta,
            patch.object(scout, "contribution_guide") as guide,
            patch.object(scout, "build_candidate", return_value={"ok": True}),
        ):
            self.assertEqual(
                scout.verify(fresh, "t", repo_cache, guide_cache, True),
                ({"ok": True}, None),
            )
        fetch_meta.assert_not_called()
        guide.assert_not_called()

    def test_verify_strategic_rejection_branch(self) -> None:
        fresh = issue(body="", title="Feature", comments=0)
        with (
            patch.object(scout, "refresh_issue", return_value=(fresh, None)),
            patch.object(bounty, "payment_signal", return_value=None),
            patch.object(scout, "supplemental_payment_signal", return_value=None),
            patch.object(scout, "strategic_rejection", return_value="support"),
        ):
            self.assertEqual(scout.verify(fresh, "t", {}, {})[1], "support")

    def test_discover_paid_platform_seen_and_rejection_branches(self) -> None:
        source_seen = "https://github.com/a/a/issues/1"
        source_reject = "https://github.com/a/a/issues/2"
        item_reject = issue(html_url=source_reject)
        with (
            patch.object(bounty, "search_github", return_value={"items": []}),
            patch.object(
                scout,
                "platform_paid_refs",
                return_value={source_seen: "sig", source_reject: "sig"},
            ),
            patch.object(scout, "issue_from_github_url", return_value=item_reject),
            patch.object(bounty, "is_clean_candidate", return_value=True),
            patch.object(scout, "verify", return_value=(None, "claimed")),
        ):
            found, rejected, examples = scout.discover_paid("t", {source_seen}, {}, {})
        self.assertEqual(found, [])
        self.assertEqual(rejected["claimed"], 1)
        self.assertEqual(examples[0]["url"], source_reject)

    def test_discover_strategic_seen_dirty_and_cached_repo_branches(self) -> None:
        seen_item = issue(html_url="https://github.com/a/a/issues/1")
        dirty = issue(html_url="https://github.com/a/a/issues/2")
        cached = issue(html_url="https://github.com/a/a/issues/3", title="Feature")
        cache = {"a/a": repo_meta()}
        with (
            patch.object(scout, "target_repo_queries", return_value=["q"]),
            patch.object(scout, "STRATEGIC_GLOBAL_QUERIES", []),
            patch.object(
                bounty,
                "search_github",
                return_value={"items": [seen_item, dirty, cached]},
            ),
            patch.object(
                bounty,
                "is_clean_candidate",
                side_effect=lambda x: x["html_url"] != dirty["html_url"],
            ),
            patch.object(bounty, "fetch_repo_metadata") as fetch_meta,
            patch.object(
                scout,
                "build_candidate",
                return_value=candidate(url=cached["html_url"], priority_score=70),
            ),
            patch.object(
                scout,
                "verify",
                return_value=(candidate(url=cached["html_url"]), None),
            ),
        ):
            found, rejected, examples = scout.discover_strategic(
                "t", {seen_item["html_url"]}, set(), cache, {}
            )
        self.assertEqual([x["url"] for x in found], [cached["html_url"]])
        fetch_meta.assert_not_called()
        self.assertEqual(rejected, {})
        self.assertEqual(examples, [])

    def test_main_keeps_higher_duplicate_and_github_report_without_examples(self) -> None:
        high = candidate(priority_score=90)
        low = candidate(priority_score=40)
        env = {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "me/repo"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(bounty, "load_seen_bounties", return_value=set()),
            patch.object(scout, "discover_paid", return_value=([high, low], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [])),
            patch.object(bounty, "create_github_issue", return_value=True) as gh,
            patch.object(bounty, "save_seen_bounties", return_value=True),
        ):
            scout.main()
        body = gh.call_args.args[3]
        self.assertNotIn("Verification rejects", body)

    def test_main_short_circuit_conditions_with_no_delivery(self) -> None:
        item_ = candidate()
        env = {"TELEGRAM_BOT_TOKEN": "tb", "GITHUB_TOKEN": "tok"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(bounty, "load_seen_bounties", return_value=set()),
            patch.object(scout, "discover_paid", return_value=([item_], {}, [])),
            patch.object(scout, "discover_strategic", return_value=([], {}, [])),
            patch.object(bounty, "send_telegram_notification") as tg,
            patch.object(bounty, "create_github_issue") as gh,
            patch.object(bounty, "save_seen_bounties") as save,
        ):
            scout.main()
        tg.assert_not_called()
        gh.assert_not_called()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
