from __future__ import annotations

import unittest
from typing import Any

import strategic_readiness as readiness


def issue(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": "Network bug",
        "body": "",
        "labels": [],
        "user": {"login": "reporter"},
    }
    item.update(overrides)
    return item


class ReadinessLabelTests(unittest.TestCase):
    def test_pending_label_dialects_and_ready_override(self) -> None:
        for labels_text in (
            "needs-triage",
            "status/triage_pending",
            "kind/bug/possible",
            "needs_analysis",
            "needs/investigation",
        ):
            with self.subTest(labels_text=labels_text):
                self.assertTrue(readiness.triage_pending_signal(labels_text))
        self.assertFalse(readiness.triage_pending_signal("enhancement"))

        self.assertEqual(
            readiness.readiness_pending_label_reason(
                issue(labels=[{"name": "status/needs-reproduction"}])
            ),
            "awaiting reproduction confirmation",
        )
        self.assertEqual(
            readiness.readiness_pending_label_reason(issue(labels=["needs/design"])),
            "awaiting maintainer design decision",
        )
        self.assertIsNone(
            readiness.readiness_pending_label_reason(
                issue(labels=["needs-investigation"]),
                ready_override=True,
            )
        )

        rotten = issue(labels=["lifecycle/rotten"])
        self.assertEqual(
            readiness.abandoned_lifecycle_reason(rotten),
            "issue is in an abandoned/rotten lifecycle state",
        )
        self.assertIsNone(readiness.abandoned_lifecycle_reason(rotten, ready_override=True))

    def test_label_set_handles_dict_string_and_empty_labels(self) -> None:
        self.assertEqual(
            readiness.issue_label_set(
                issue(labels=[{"name": " Help Wanted "}, "BUG", {"name": ""}, ""])
            ),
            {"help wanted", "bug"},
        )

    def test_proposal_stage_recognizes_title_and_label_dialects(self) -> None:
        self.assertTrue(readiness.proposal_stage_signal(issue(title="RFC: retry policy")))
        self.assertTrue(
            readiness.proposal_stage_signal(issue(labels=[{"name": "kind/proposal"}]))
        )
        self.assertTrue(readiness.proposal_stage_signal(issue(labels=["needs/discussion"])))
        self.assertFalse(readiness.proposal_stage_signal(issue(labels=["enhancement"])))


class MaintainerReadinessTests(unittest.TestCase):
    def test_authority_requires_maintainer_or_explicit_project_action(self) -> None:
        self.assertTrue(
            readiness.maintainer_comment_authority(
                {"body": "Thanks.", "author_association": "MEMBER"}
            )
        )
        self.assertTrue(
            readiness.maintainer_comment_authority(
                {
                    "body": "I'm going to hand it off to the Engineering team.",
                    "author_association": "CONTRIBUTOR",
                }
            )
        )
        self.assertFalse(
            readiness.maintainer_comment_authority(
                {"body": "Thanks.", "author_association": "CONTRIBUTOR"}
            )
        )
        self.assertFalse(
            readiness.maintainer_comment_authority(
                {
                    "body": "We'll reevaluate this.",
                    "author_association": "NONE",
                }
            )
        )

    def test_requested_diagnostic_is_cleared_when_evidence_arrives(self) -> None:
        comments = [
            {
                "body": "Could you provide a heap profile from the affected process?",
                "author_association": "MEMBER",
            },
            {
                "body": "Here’s the requested heap profile attached with the logs.",
                "author_association": "NONE",
            },
        ]
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), comments),
            (None, None),
        )

    def test_latest_explicit_stance_wins(self) -> None:
        hold = {
            "body": "Please wait before implementing; this needs clarification.",
            "author_association": "MEMBER",
        }
        ready = {
            "body": "Clarification is complete. This is ready for implementation.",
            "author_association": "MEMBER",
        }

        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), [hold, ready]),
            (True, None),
        )
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), [ready, hold]),
            (False, "maintainer asked contributors to wait before implementation"),
        )

    def test_redirect_detection_requires_actual_redirection(self) -> None:
        context_only = [
            {
                "body": "Related code lives in distribution/reference for context.",
                "author_association": "MEMBER",
            }
        ]
        redirect = [
            {
                "body": (
                    "This requires a change in https://github.com/distribution/reference. "
                    "Please open an issue there."
                ),
                "author_association": "COLLABORATOR",
            }
        ]

        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), context_only),
            (None, None),
        )
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), redirect),
            (
                False,
                "maintainer redirected implementation/discussion to another project",
            ),
        )

    def test_duplicate_detection_avoids_related_and_not_duplicate_text(self) -> None:
        duplicate = [
            {
                "body": "Looks like a possible duplicate of #123; discussion is tracked there.",
                "author_association": "OWNER",
            }
        ]
        distinct = [
            {
                "body": "This is not a duplicate of #123; the symptoms are different.",
                "author_association": "OWNER",
            }
        ]
        untrusted = [
            {
                "body": "Looks like a duplicate of #123.",
                "author_association": "NONE",
            }
        ]

        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), duplicate),
            (
                False,
                "maintainer indicates this is probably tracked by another canonical issue",
            ),
        )
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), distinct),
            (None, None),
        )
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), untrusted),
            (None, None),
        )

    def test_feedback_language_only_blocks_proposal_stage(self) -> None:
        comment = [
            {
                "body": "We want to gather feedback before committing.",
                "author_association": "OWNER",
            }
        ]
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), comment),
            (None, None),
        )
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(
                issue(labels=[{"name": "kind/proposal"}]),
                comment,
            ),
            (False, "proposal is still gathering feedback"),
        )


class LifecycleClassificationTests(unittest.TestCase):
    def test_dependency_dashboard_requires_automated_tracking_context(self) -> None:
        dashboard = issue(
            title="Dependency Dashboard",
            labels=[{"name": "dependencies"}],
            user={"login": "renovate-sh-app[bot]"},
        )
        self.assertEqual(
            readiness.automated_tracking_issue_reason(dashboard),
            "automated dependency dashboard, not an implementation task",
        )
        self.assertIsNone(
            readiness.automated_tracking_issue_reason(
                issue(title="Dependency Dashboard", user={"login": "human"})
            )
        )
        self.assertIsNone(
            readiness.automated_tracking_issue_reason(
                issue(title="Nightly status", user={"login": "example[bot]"})
            )
        )

    def test_release_tracking_and_release_only_work_are_distinct(self) -> None:
        self.assertEqual(
            readiness.release_tracking_reason(issue(title="Release 2.0 tracking checklist")),
            "release planning/tracking issue, not implementation work",
        )

        implemented = issue(
            body=(
                "PR #123 merged last week. The release tag has not yet been published. "
                "Request: please tag a new release."
            )
        )
        self.assertEqual(
            readiness.release_tracking_reason(implemented),
            "implementation already merged; only release/tagging remains",
        )

        self.assertIsNone(
            readiness.release_tracking_reason(issue(body="PR #123 merged last week."))
        )
        self.assertIsNone(
            readiness.release_tracking_reason(issue(body="Please publish a new release."))
        )
        self.assertIsNone(
            readiness.release_tracking_reason(issue(title="Bug in release parser"))
        )


if __name__ == "__main__":
    unittest.main()
