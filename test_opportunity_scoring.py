from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

import opportunity_scoring as scoring


AMOUNT_RE = r"[$][ ]*[0-9][0-9,]*(?:[.][0-9]+)?"


def issue(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "html_url": "https://github.com/example/project/issues/42",
        "title": "Network regression",
        "body": "",
        "labels": [],
        "comments": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    item.update(overrides)
    return item


def repo_meta(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "stargazers_count": 5000,
        "language": "Go",
        "pushed_at": datetime.now(timezone.utc).isoformat(),
    }
    data.update(overrides)
    return data


class EffortCalibrationTests(unittest.TestCase):
    def test_type_feature_dialect_no_longer_falls_into_quick_bug_bucket(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Add response metadata header",
                body="Expose one additional response metadata option.",
                labels=[{"name": "type/feature"}],
            )
        )
        self.assertEqual(estimate.bucket, "6–12h")
        self.assertIn("feature/enhancement scope", estimate.reasons)

    def test_cross_component_feature_is_large_scope(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Allow separate buckets for chunks and index",
                labels=[{"name": "type/feature"}],
                body=(
                    "Storage configuration currently uses one bucket for chunks and index. "
                    "The compactor needs a separate index bucket while the chunk bucket remains "
                    "locked. The schema and configuration need to support both stores."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "1d+")
        self.assertEqual(
            estimate.reasons,
            ("feature spans multiple runtime/configuration components",),
        )

    def test_compatibility_sensitive_persisted_state_is_large_scope(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Replace legacy member ID digest",
                labels=[{"name": "type/feature"}],
                body=(
                    "A direct change is backward-incompatible with existing deployments. "
                    "Member IDs are persisted in the WAL and snapshots and exchanged during "
                    "peer handshakes, so old clusters must still rejoin and recover."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "1d+")
        self.assertIn("backward-compatibility", estimate.reasons[0])

    def test_fenced_diagnostics_do_not_inflate_effort(self) -> None:
        fence = chr(96) * 3
        huge_dump = "x" * 18000
        estimate = scoring.estimate_effort_details(
            issue(
                title="Certain log configuration reliably triggers a segfault",
                body=(
                    "The crash is deterministic. It may be related to an upstream issue.\n"
                    + fence
                    + "\n"
                    + huge_dump
                    + "\n"
                    + fence
                ),
            )
        )
        self.assertEqual(estimate.bucket, "3–6h")
        self.assertEqual(estimate.reasons, ("upstream/dependency investigation",))

    def test_comment_volume_is_competition_not_effort(self) -> None:
        item = issue(
            title="TCP mode leaks upstream connection",
            body="Deterministic leak: the upstream connection never closes.",
            comments=25,
        )
        self.assertEqual(scoring.estimate_effort(item), "1–3h")
        self.assertEqual(scoring.competition(item), "high")

    def test_concurrency_bug_has_debugging_floor(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Manager logs after shutdown",
                body=(
                    "The manager continues work after Start returns and can trigger a data race "
                    "when a test writer has already closed."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "3–6h")
        self.assertEqual(estimate.reasons, ("concurrency/lifecycle debugging risk",))

    def test_localized_todo_can_be_quick_without_generic_fix_keyword(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Refactor writeHeaders confirmation status",
                body=(
                    "exp/api/remote_headers.go contains a TODO in the writeHeaders method. "
                    "The method can use the confirmed state instead of parsing message type."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "1–3h")
        self.assertEqual(estimate.reasons, ("localized TODO/code-path change",))

    def test_docs_microfix_and_broader_docs_are_distinct(self) -> None:
        micro = issue(title="README typo", body="Fix spelling in the README.")
        broader = issue(
            title="docs: explain controller behavior",
            body="Document controller lifecycle and operational tradeoffs.",
        )
        self.assertEqual(scoring.estimate_effort(micro), "<1h")
        self.assertEqual(scoring.estimate_effort(broader), "1–3h")

    def test_existing_broad_scope_signals_remain_conservative(self) -> None:
        cases = (
            (issue(title="Architecture rewrite"), "1d+"),
            (
                issue(
                    title="Intermittent ENI race",
                    body="We haven't been able to reproduce on demand; low-probability race.",
                ),
                "1d+",
            ),
            (
                issue(
                    title="Broad logger cleanup",
                    body="a.go b.go c.go d.go",
                ),
                "6–12h",
            ),
            (
                issue(
                    title="Watcher backlog",
                    body="Suggested fixes\n- first\n- second\n- third",
                ),
                "6–12h",
            ),
            (issue(title="Ordinary bug", body="normal report"), "3–6h"),
        )
        for item, expected in cases:
            with self.subTest(title=item["title"]):
                self.assertEqual(scoring.estimate_effort(item), expected)

    def test_feature_request_and_environment_heavy_signals_remain_large(self) -> None:
        self.assertEqual(
            scoring.estimate_effort(issue(title="FR: Support ExternalName", body="small request")),
            "1d+",
        )
        self.assertEqual(
            scoring.estimate_effort(
                issue(
                    title="Android DNS regression",
                    body="dual SIM physical device reproduction",
                )
            ),
            "1d+",
        )

    def test_large_prose_is_distinct_from_large_fenced_dump(self) -> None:
        self.assertEqual(
            scoring.estimate_effort(issue(title="Large design", body="a" * 13001)),
            "1d+",
        )
        self.assertEqual(
            scoring.estimate_effort(issue(title="Broad bug", body="a" * 7000)),
            "6–12h",
        )


class ScoringRegressionTests(unittest.TestCase):
    def test_candidate_exposes_effort_reasons(self) -> None:
        result = scoring.build_candidate(
            issue(
                title="TCP mode leaks upstream connection",
                body="Deterministic leak: the upstream connection never closes.",
            ),
            "strategic",
            None,
            repo_meta(),
            "guide",
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertEqual(result["effort"], "1–3h")
        self.assertEqual(
            result["effort_reasons"],
            ["bounded deterministic bug signal"],
        )
        self.assertEqual(result["priority_score"], result["career_score"])

    def test_paid_expected_value_uses_estimated_effort(self) -> None:
        result = scoring.build_candidate(
            issue(title="README typo", body="Fix spelling."),
            "paid",
            "confirmed bounty platform feed: $90",
            repo_meta(stargazers_count=100),
            None,
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertEqual(result["effort"], "<1h")
        self.assertEqual(result["reward"], "$90")
        self.assertEqual(result["expected_hourly"], 120.0)
        self.assertGreater(result["cash_score"], 0)

    def test_strategic_freshness_and_maintainer_activity_still_score(self) -> None:
        now = datetime.now(timezone.utc)
        result = scoring.build_candidate(
            issue(
                title="Network regression",
                body="network regression",
                comments=1,
                created_at=(now - timedelta(days=900)).isoformat(),
                updated_at=(now - timedelta(days=120)).isoformat(),
            ),
            "strategic",
            None,
            repo_meta(),
            None,
            [
                {
                    "created_at": (now - timedelta(days=10)).isoformat(),
                    "author_association": "MEMBER",
                }
            ],
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("recent maintainer activity", result["career_reasons"])
        self.assertNotIn("stale inactive backlog penalty", result["career_reasons"])

    def test_inactive_old_issue_penalty_remains(self) -> None:
        now = datetime.now(timezone.utc)
        result = scoring.build_candidate(
            issue(
                title="Network bug",
                body="network regression",
                created_at=(now - timedelta(days=900)).isoformat(),
                updated_at=(now - timedelta(days=500)).isoformat(),
            ),
            "strategic",
            None,
            repo_meta(),
            None,
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("stale inactive backlog penalty", result["career_reasons"])

    def test_payment_and_activity_helpers_preserve_public_behavior(self) -> None:
        self.assertEqual(scoring.payment_confidence(None), 0)
        self.assertEqual(
            scoring.payment_confidence("confirmed bounty platform feed"),
            100,
        )
        self.assertEqual(
            scoring.reward_text("confirmed bounty platform: $25", AMOUNT_RE),
            "$25",
        )
        self.assertIsNone(scoring.reward_text(None, AMOUNT_RE))
        self.assertEqual(scoring.effort_hours("6–12h"), 9.0)

        missing = scoring.repo_activity({"pushed_at": None})
        self.assertEqual(missing, "unknown")


if __name__ == "__main__":
    unittest.main()
