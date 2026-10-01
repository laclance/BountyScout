from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import opportunity_scout as scout
import github_access as github
import scout_bounties as bounty
import strategic_competition as competition


def issue(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "html_url": "https://github.com/example/project/issues/42",
        "title": "Network regression",
        "body": "",
        "comments": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    item.update(overrides)
    return item


class CompatibilityWrapperTests(unittest.TestCase):
    def test_opportunity_scout_preserves_claim_recency_export(self) -> None:
        self.assertTrue(scout.claim_source_is_recent({}))
        self.assertEqual(
            scout.STRATEGIC_CLAIM_MAX_AGE_DAYS,
            competition.STRATEGIC_CLAIM_MAX_AGE_DAYS,
        )


class ClaimCompetitionTests(unittest.TestCase):
    def test_claim_recency_uses_issue_creation_and_comment_updates(self) -> None:
        recent = datetime.now(timezone.utc)
        stale = recent - timedelta(days=competition.STRATEGIC_CLAIM_MAX_AGE_DAYS + 1)

        self.assertTrue(
            competition.claim_source_is_recent(
                {"created_at": recent.isoformat()},
                issue_body=True,
            )
        )
        self.assertFalse(
            competition.claim_source_is_recent(
                {"created_at": stale.isoformat()},
                issue_body=True,
            )
        )
        self.assertTrue(
            competition.claim_source_is_recent(
                {
                    "created_at": stale.isoformat(),
                    "updated_at": recent.isoformat(),
                }
            )
        )
        self.assertTrue(competition.claim_source_is_recent({}))

    def test_strategic_claim_reason_ignores_stale_and_third_person_work(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        stale = (
            datetime.now(timezone.utc)
            - timedelta(days=competition.STRATEGIC_CLAIM_MAX_AGE_DAYS + 1)
        ).isoformat()

        self.assertEqual(
            competition.strategic_claim_reason(
                issue(body="I've implemented this locally.", created_at=recent),
                [],
            ),
            "issue author already has an implementation/fix in progress",
        )
        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(body="I've implemented this locally.", created_at=stale),
                [],
            )
        )
        self.assertEqual(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": "I'm working on a fix.",
                        "updated_at": recent,
                        "user": {"login": "dev"},
                    }
                ],
            ),
            "active claim by @dev",
        )
        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": "Someone else is working on a fix.",
                        "updated_at": recent,
                        "user": {"login": "observer"},
                    },
                    {
                        "body": "I'm working on a fix.",
                        "updated_at": stale,
                        "user": {"login": "old-dev"},
                    },
                ],
            )
        )

    def test_live_claim_wording_pick_up_and_willing_to_contribute_pr(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()

        self.assertEqual(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": (
                            "I'd like to pick this up. On current main, the stats are "
                            "not retained. I'll wait for direction before publishing an implementation."
                        ),
                        "updated_at": recent,
                        "user": {"login": "fzlzjerry"},
                    }
                ],
            ),
            "active claim by @fzlzjerry",
        )

        self.assertEqual(
            competition.strategic_claim_reason(
                issue(
                    body=(
                        "Contribution Intention (Optional)\n\n"
                        "- [x] Yes, I am willing to contribute a PR to implement this feature\n"
                        "- [ ] No, I cannot work on a PR at this time"
                    ),
                    created_at=recent,
                ),
                [],
            ),
            "issue author already has an implementation/fix in progress",
        )

        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": "I'd like to see someone pick this up.",
                        "updated_at": recent,
                        "user": {"login": "observer"},
                    }
                ],
            )
        )

    def test_working_branch_with_test_is_active_implementation_claim(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        self.assertEqual(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": (
                            "The quick fix mirrors the token suppression, and I have that "
                            "working with a test on a branch. I am not opening a PR yet because "
                            "I want maintainer direction on the more accurate fix."
                        ),
                        "updated_at": recent,
                        "user": {"login": "david"},
                    }
                ],
            ),
            "active claim by @david",
        )

    def test_local_exploration_with_concrete_changes_is_active_implementation(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        self.assertEqual(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": (
                            "I poked at this locally. No breaking change needed. "
                            "I made the explicit selector win and moved the preference logic "
                            "into sdk-metrics."
                        ),
                        "updated_at": recent,
                        "user": {"login": "neoLsH"},
                    }
                ],
            ),
            "active claim by @neoLsH",
        )
        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": "I poked at this locally but did not change anything.",
                        "updated_at": recent,
                        "user": {"login": "observer"},
                    }
                ],
            )
        )

    def test_issue_numbered_branch_link_by_author_is_active_implementation(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        self.assertEqual(
            competition.strategic_claim_reason(
                issue(html_url="https://github.com/nodejs/undici/issues/5912", body=""),
                [
                    {
                        "body": "https://github.com/KhafraDev/undici/tree/fetch/issue-5912",
                        "updated_at": recent,
                        "user": {"login": "KhafraDev"},
                        "author_association": "MEMBER",
                    }
                ],
            ),
            "active implementation branch linked by @KhafraDev",
        )
        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(html_url="https://github.com/nodejs/undici/issues/5912", body=""),
                [
                    {
                        "body": "https://github.com/SomeoneElse/undici/tree/fetch/issue-5912",
                        "updated_at": recent,
                        "user": {"login": "observer"},
                    }
                ],
            )
        )

    def test_claim_reason_handles_unidentifiable_issue_without_branch_matching(self) -> None:
        self.assertIsNone(
            competition.strategic_claim_reason(
                {"html_url": "bad", "body": ""},
                [{"body": "Thanks for the report.", "user": {"login": "observer"}}],
            )
        )

    def test_supplemental_claims_require_first_person_ownership_language(self) -> None:
        self.assertEqual(
            competition.supplemental_claim_reason(
                issue(),
                [{"body": "I can take this one.", "user": {"login": "dev"}}],
            ),
            "active claim by @dev",
        )
        self.assertEqual(
            competition.supplemental_claim_reason(
                issue(),
                [{"body": "Planning a fix."}],
            ),
            "active claim by @someone",
        )
        self.assertIsNone(
            competition.supplemental_claim_reason(
                issue(),
                [
                    {"body": "I can review a PR for this."},
                    {"body": "Someone should work on a fix."},
                    {"body": "Thanks for the report."},
                ],
            )
        )
        self.assertIsNone(competition.supplemental_claim_reason(issue(comments=0), []))


class LinkedPullRequestTests(unittest.TestCase):
    def test_linked_pr_checks_open_state_and_deduplicates_candidates(self) -> None:
        comments = [
            {"body": ("submitted PR #10; implementation pull request #11; also submitted PR #10")}
        ]
        with patch.object(
            github,
            "github_get",
            side_effect=[
                {"state": "closed", "html_url": "https://github.com/example/project/pull/10"},
                {"state": "open", "html_url": "https://github.com/example/project/pull/11"},
            ],
        ) as getter:
            self.assertEqual(
                competition.linked_open_pr_reason(issue(), "t", comments),
                "existing open implementation PR: https://github.com/example/project/pull/11",
            )
            self.assertEqual(getter.call_count, 2)

    def test_linked_pr_uses_same_repo_urls_and_ignores_non_pr_api_results(self) -> None:
        comments = [
            {
                "body": (
                    "See https://github.com/other/project/pull/88 for background. "
                    "Related fix #12 may also help."
                )
            }
        ]
        with patch.object(github, "github_get", return_value=[]):
            self.assertIsNone(competition.linked_open_pr_reason(issue(), "t", comments))

        same_repo = [
            {
                "body": "Implementation: https://github.com/example/project/pull/13",
            }
        ]
        with patch.object(
            github,
            "github_get",
            return_value={"state": "open"},
        ):
            self.assertEqual(
                competition.linked_open_pr_reason(issue(), "t", same_repo),
                "existing open implementation PR: https://github.com/example/project/pull/13",
            )

    def test_linked_pr_detects_implementation_link_in_issue_body_without_comments(self) -> None:
        item = issue(
            comments=0,
            body=(
                "PR https://github.com/example/project/pull/6293 "
                "fixes exactly this and has been open for review."
            ),
        )
        with patch.object(
            github,
            "github_get",
            return_value={
                "state": "open",
                "html_url": "https://github.com/example/project/pull/6293",
            },
        ) as getter:
            self.assertEqual(
                competition.linked_open_pr_reason(item, "t", []),
                "existing open implementation PR: https://github.com/example/project/pull/6293",
            )
        getter.assert_called_once()

        background = issue(
            comments=0,
            body=(
                "For historical context see "
                "https://github.com/example/project/pull/111 from the earlier refactor."
            ),
        )
        with patch.object(github, "github_get") as no_fetch:
            self.assertIsNone(competition.linked_open_pr_reason(background, "t", []))
        no_fetch.assert_not_called()

    def test_linked_pr_short_circuits_invalid_issue_and_zero_comments(self) -> None:
        with patch.object(github, "github_get") as getter:
            self.assertIsNone(
                competition.linked_open_pr_reason(
                    {"html_url": "bad", "comments": 1},
                    "t",
                    [],
                )
            )
            self.assertIsNone(competition.linked_open_pr_reason(issue(comments=0), "t", []))
            getter.assert_not_called()


class SearchPullRequestTests(unittest.TestCase):
    def test_search_only_runs_when_thread_hints_at_implementation(self) -> None:
        with patch.object(github, "github_get") as getter:
            self.assertIsNone(
                competition.search_open_implementation_pr_reason(
                    issue(body="No competing work is known."),
                    "t",
                    [],
                )
            )
            getter.assert_not_called()

    def test_search_requires_real_closing_reference_to_exact_issue(self) -> None:
        results = {
            "items": [
                {
                    "pull_request": {"url": "x"},
                    "title": "Unrelated issue number prefix",
                    "body": "Fixes #420",
                    "html_url": "https://github.com/example/project/pull/1",
                },
                {
                    "pull_request": {"url": "x"},
                    "title": "Only related",
                    "body": "Related to #42",
                    "html_url": "https://github.com/example/project/pull/2",
                },
                {
                    "pull_request": {"url": "x"},
                    "title": "Other repository URL",
                    "body": "Closes https://github.com/other/project/issues/42",
                    "html_url": "https://github.com/example/project/pull/3",
                },
                {
                    "pull_request": {"url": "x"},
                    "title": "Exact implementation",
                    "body": "Resolves issue #42",
                    "html_url": "https://github.com/example/project/pull/4",
                },
            ]
        }
        with patch.object(github, "github_get", return_value=results) as getter:
            self.assertEqual(
                competition.search_open_implementation_pr_reason(
                    issue(body="A pull request may already exist."),
                    "t",
                    [],
                ),
                "existing open implementation PR: https://github.com/example/project/pull/4",
            )
            url = getter.call_args.args[0]
            self.assertIn("is%3Apr", url)
            self.assertIn("is%3Aopen", url)

    def test_search_ignores_non_pr_results_and_missing_urls(self) -> None:
        cases: list[Any] = [
            {"items": ["invalid"]},
            {
                "items": [
                    {
                        "title": "Issue result",
                        "body": "Fixes #42",
                        "html_url": "https://github.com/example/project/issues/99",
                    },
                    {
                        "pull_request": {"url": "x"},
                        "title": "No URL",
                        "body": "Fixes #42",
                        "html_url": "",
                    },
                ]
            },
        ]
        for response in cases:
            with (
                self.subTest(response=response),
                patch.object(
                    github,
                    "github_get",
                    return_value=response,
                ),
            ):
                self.assertIsNone(
                    competition.search_open_implementation_pr_reason(
                        issue(body="There is a draft patch."),
                        "t",
                        [],
                    )
                )

        self.assertIsNone(
            competition.search_open_implementation_pr_reason(
                {"html_url": "bad", "body": "PR exists"},
                "t",
                [],
            )
        )

    def test_search_failure_fails_closed_when_pr_check_is_needed(self) -> None:
        responses: tuple[Any, ...] = (None, [])
        for response in responses:
            with (
                self.subTest(response=response),
                patch.object(github, "github_get", return_value=response),
            ):
                self.assertEqual(
                    competition.search_open_implementation_pr_reason(
                        issue(body="There may already be an implementation PR."),
                        "t",
                        [],
                    ),
                    "could not verify open implementation PR search",
                )


class TimelinePullRequestTests(unittest.TestCase):
    def test_timeline_requires_open_cross_referenced_pull_request_with_url(self) -> None:
        timeline = [
            {"event": "commented"},
            {"event": "cross-referenced", "source": "bad"},
            {"event": "cross-referenced", "source": {"issue": "bad"}},
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {"url": "x"},
                        "state": "closed",
                        "html_url": "https://github.com/example/project/pull/8",
                    }
                },
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {"url": "x"},
                        "state": "open",
                        "html_url": "https://github.com/example/project/pull/9",
                    }
                },
            },
        ]
        with patch.object(github, "github_get", return_value=timeline):
            self.assertEqual(
                competition.timeline_open_pr_reason(issue(), "t"),
                "existing open implementation PR: https://github.com/example/project/pull/9",
            )

        with patch.object(github, "github_get", return_value={}):
            self.assertIsNone(competition.timeline_open_pr_reason(issue(), "t"))
        self.assertIsNone(competition.timeline_open_pr_reason({"html_url": "bad"}, "t"))


class CompetitionOrchestrationTests(unittest.TestCase):
    def test_strategic_competition_precedence_is_pr_then_claim_then_search(self) -> None:
        with patch.object(
            bounty,
            "has_existing_implementation_pr",
            return_value="timeline/search pr",
        ):
            self.assertEqual(
                competition.strategic_competition_reason(
                    issue(),
                    "t",
                    [],
                    linked_pr_checker=lambda *_: "linked pr",
                    strategic_claim_checker=lambda *_: "claim",
                    search_pr_checker=lambda *_: "searched pr",
                ),
                "timeline/search pr",
            )

        with patch.object(bounty, "has_existing_implementation_pr", return_value=None):
            self.assertEqual(
                competition.strategic_competition_reason(
                    issue(),
                    "t",
                    [],
                    linked_pr_checker=lambda *_: "linked pr",
                    strategic_claim_checker=lambda *_: "claim",
                    search_pr_checker=lambda *_: "searched pr",
                ),
                "linked pr",
            )
            self.assertEqual(
                competition.strategic_competition_reason(
                    issue(),
                    "t",
                    [],
                    linked_pr_checker=lambda *_: None,
                    strategic_claim_checker=lambda *_: "claim",
                    search_pr_checker=lambda *_: "searched pr",
                ),
                "claim",
            )
            self.assertEqual(
                competition.strategic_competition_reason(
                    issue(),
                    "t",
                    [],
                    linked_pr_checker=lambda *_: None,
                    strategic_claim_checker=lambda *_: None,
                    search_pr_checker=lambda *_: "searched pr",
                ),
                "searched pr",
            )

    def test_extended_competition_preserves_paid_claim_rules_then_supplemental(self) -> None:
        with patch.object(bounty, "has_existing_implementation_pr", return_value=None):
            self.assertEqual(
                competition.extended_competition_reason(
                    issue(),
                    "t",
                    [{"body": "I'm working on this", "user": {"login": "dev"}}],
                    linked_pr_checker=lambda *_: None,
                ),
                "active claim by @dev",
            )
            self.assertEqual(
                competition.extended_competition_reason(
                    issue(),
                    "t",
                    [{"body": "Planning a fix", "user": {"login": "dev2"}}],
                    linked_pr_checker=lambda *_: None,
                ),
                "active claim by @dev2",
            )
            self.assertIsNone(
                competition.extended_competition_reason(
                    issue(),
                    "t",
                    [{"body": "I can review a PR."}],
                    linked_pr_checker=lambda *_: None,
                )
            )

    def test_competition_rejects_unidentifiable_issue_without_network_calls(self) -> None:
        bad = {"html_url": "bad", "comments": 1}
        with patch.object(bounty, "has_existing_implementation_pr") as existing:
            self.assertEqual(
                competition.strategic_competition_reason(bad, "t", []),
                "could not identify repository/issue number",
            )
            self.assertEqual(
                competition.extended_competition_reason(bad, "t", []),
                "could not identify repository/issue number",
            )
            existing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
