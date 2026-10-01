from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

import opportunity_scoring as scoring
import opportunity_scout as scout


AMOUNT_RE = r"[$][ ]*[0-9][0-9,]*(?:[.][0-9]+)?"

CHAIN_LOVE_3969_BODY = """At current main, actionButtons is stored as a JSON array of Markdown links.
Examples include references/offers/mcpservers.csv where Website and Docs can point to the
same destination.

Add a validator rule for actionButtons:
1. Parse the JSON array.
2. Parse each Markdown-link item into label + destination.
3. Normalize trivial URL spelling differences for comparison.
4. Reject duplicate normalized destination URLs inside the same cell.
5. Error output should name the file, row/slug, repeated URL, and colliding labels.

Existing rows can be corrected in bounded follow-up PRs after maintainers decide whether each
repeated destination is redundant or whether a better official docs/product URL exists.
"""


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

    def test_feature_with_multiple_file_refs_is_cross_component(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Add scoped feature flag",
                labels=[{"name": "type/feature"}],
                body="pkg/a.go pkg/b.go pkg/c.go",
            )
        )
        self.assertEqual(estimate.bucket, "1d+")
        self.assertIn("multiple runtime/configuration components", estimate.reasons[0])

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

    def test_cross_service_generated_documentation_is_not_bounded_docs(self) -> None:
        estimate = scoring.estimate_effort_details(
            issue(
                title="Fragmented gRPC services documentation",
                labels=[{"name": "kind/feature"}],
                body=(
                    "Documentation is fragmented across proto files and the website. "
                    "We want rich documentation for all gRPC services in one place. "
                    "These could potentially be generated docs and hosted on the website."
                ),
            )
        )
        self.assertEqual(estimate.bucket, "6–12h")
        self.assertEqual(
            estimate.reasons,
            ("cross-service documentation/generation scope",),
        )

    def test_docs_microfix_and_broader_docs_are_distinct(self) -> None:
        micro = issue(title="README typo", body="Fix spelling in the README.")
        broader = issue(
            title="docs: explain controller behavior",
            body="Document controller lifecycle and operational tradeoffs.",
        )
        self.assertEqual(scoring.estimate_effort(micro), "<1h")
        self.assertEqual(scoring.estimate_effort(broader), "1–3h")

    def test_chain_love_3969_is_not_a_documentation_microfix(self) -> None:
        item = issue(
            title="[DBIP] Reject duplicate actionButtons destinations within the same cell",
            body=CHAIN_LOVE_3969_BODY,
        )
        self.assertFalse(scoring.documentation_microfix(item))
        estimate = scoring.estimate_effort_details(item)
        self.assertEqual(estimate.bucket, "3–6h")
        self.assertEqual(estimate.reasons, ("moderate implementation scope",))

    def test_incidental_docs_path_does_not_make_implementation_a_microfix(self) -> None:
        item = issue(
            title="Reject duplicate action button destinations",
            body=(
                "Update the validator for actionButtons. One example is docs/generated/table.md, "
                "but the task is to reject duplicate destinations at validation time."
            ),
        )
        self.assertFalse(scoring.documentation_microfix(item))
        self.assertNotEqual(scoring.estimate_effort(item), "<1h")

    def test_spelling_differences_inside_implementation_prose_do_not_trigger_microfix(self) -> None:
        item = issue(
            title="Normalize action button destinations",
            body=(
                "Parse the JSON array and normalize URL spelling differences before comparing "
                "destinations. Emit a structured validation error for duplicates."
            ),
        )
        self.assertFalse(scoring.documentation_microfix(item))
        self.assertNotEqual(scoring.estimate_effort(item), "<1h")

    def test_legitimate_tiny_documentation_fixes_remain_microfixes(self) -> None:
        cases = (
            issue(title="Fix typo in README"),
            issue(title="Correct spelling in docs"),
            issue(title="Fix broken documentation link"),
            issue(title="Repair broken image in documentation"),
        )
        for item in cases:
            with self.subTest(title=item["title"]):
                self.assertTrue(scoring.documentation_microfix(item))
                self.assertEqual(scoring.estimate_effort(item), "<1h")

    def test_docs_task_with_validator_or_parser_scope_is_not_a_microfix(self) -> None:
        item = issue(
            title="docs: fix spelling in actionButtons guide",
            body=(
                "Also add validator logic, parse the JSON payload, and emit structured validation "
                "errors so the documented rule is enforced at runtime."
            ),
        )
        self.assertFalse(scoring.documentation_microfix(item))
        self.assertNotEqual(scoring.estimate_effort(item), "<1h")

    def test_generic_clipboard_api_does_not_count_as_infrastructure_domain_fit(self) -> None:
        ui = scoring.build_candidate(
            issue(
                title="Show Copied only after clipboard write succeeds",
                body=(
                    "navigator.clipboard.writeText may reject when the clipboard API "
                    "is unavailable. Keep the UI usable and add a Vitest regression."
                ),
                labels=[{"name": "bug"}, {"name": "good first issue"}],
            ),
            "strategic",
            None,
            repo_meta(stargazers_count=18, language="TypeScript"),
            "guide",
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertNotIn("target infrastructure/domain fit", ui["career_reasons"])

        backend = scoring.build_candidate(
            issue(
                title="Validate REST API endpoint input",
                body="The REST API endpoint should reject malformed addresses.",
            ),
            "strategic",
            None,
            repo_meta(stargazers_count=18, language="TypeScript"),
            "guide",
            target_repos=set(),
            amount_pattern=AMOUNT_RE,
        )
        self.assertIn("target infrastructure/domain fit", backend["career_reasons"])

    def test_architecture_environment_metadata_is_not_design_scope(self) -> None:
        cloudflared = issue(
            title="QUIC Hijack() skips the status-written check that HTTP/2 enforces",
            body=(
                "HTTP/2 refuses Hijack when status has not been written, but QUIC does not. "
                "Both transports should enforce the same precondition. "
                "OS: Linux. Architecture: AMD64. Version: 2026.9.1."
            ),
        )
        estimate = scoring.estimate_effort_details(cloudflared)
        self.assertEqual(estimate.bucket, "3–6h")
        self.assertEqual(estimate.reasons, ("moderate implementation scope",))

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
    def test_opportunity_scout_wrapper_and_report_expose_effort_basis(self) -> None:
        item = issue(
            title="TCP mode leaks upstream connection",
            body="Deterministic leak: the upstream connection never closes.",
        )
        details = scout.estimate_effort_details(item)
        self.assertEqual(details.bucket, "1–3h")

        result = scoring.build_candidate(
            item,
            "strategic",
            None,
            repo_meta(),
            "guide",
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        rendered = scout.markdown_candidate(result, 1)
        self.assertIn("**Priority score:**", rendered)
        self.assertIn("**Effort basis:** bounded deterministic bug signal", rendered)
        self.assertIn(
            "**Priority basis:** 1–3h execution bonus, no visible competition bonus",
            rendered,
        )

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
        self.assertGreater(result["priority_score"], result["career_score"])
        self.assertEqual(
            result["priority_reasons"],
            ["1–3h execution bonus", "no visible competition bonus"],
        )

    def test_strategic_priority_favors_execution_fit_when_career_is_nearly_equal(self) -> None:
        containerd_priority, containerd_reasons = scoring.strategic_priority_score(
            80,
            "1–3h",
            "none",
        )
        terraform_priority, terraform_reasons = scoring.strategic_priority_score(
            81,
            "3–6h",
            "medium",
        )

        self.assertEqual(containerd_priority, 93)
        self.assertEqual(terraform_priority, 79)
        self.assertGreater(containerd_priority, terraform_priority)
        self.assertEqual(
            containerd_reasons,
            ["1–3h execution bonus", "no visible competition bonus"],
        )
        self.assertEqual(
            terraform_reasons,
            ["3–6h execution bonus", "medium competition penalty"],
        )

    def test_strategic_priority_still_allows_large_career_gap_to_win(self) -> None:
        high_value, _ = scoring.strategic_priority_score(95, "6–12h", "low")
        easy_but_weaker, _ = scoring.strategic_priority_score(70, "1–3h", "none")
        self.assertGreater(high_value, easy_but_weaker)

    def test_strategic_priority_caps_extreme_adjustments(self) -> None:
        self.assertEqual(scoring.strategic_priority_score(100, "<1h", "none")[0], 100)
        self.assertEqual(scoring.strategic_priority_score(5, "1d+", "high")[0], 0)

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

    def test_bot_only_activity_does_not_revive_old_issue(self) -> None:
        now = datetime.now(timezone.utc)
        bot_time = now - timedelta(days=12)
        result = scoring.build_candidate(
            issue(
                title='Make "Bump etcd Version in Kubernetes" part of the release process',
                body="Release process enhancement.",
                created_at=(now - timedelta(days=540)).isoformat(),
                updated_at=bot_time.isoformat(),
                comments=2,
                labels=[{"name": "stale"}],
            ),
            "strategic",
            None,
            repo_meta(),
            None,
            [
                {
                    "created_at": (now - timedelta(days=525)).isoformat(),
                    "author_association": "MEMBER",
                    "user": {"login": "human-maintainer"},
                },
                {
                    "created_at": bot_time.isoformat(),
                    "updated_at": bot_time.isoformat(),
                    "author_association": "CONTRIBUTOR",
                    "user": {"login": "github-actions[bot]"},
                    "body": "This issue has been automatically marked as stale.",
                },
                {
                    "created_at": (bot_time - timedelta(days=30)).isoformat(),
                    "author_association": "CONTRIBUTOR",
                    "user": {"login": "stale[bot]"},
                    "body": "Older automated stale reminder.",
                },
            ],
            target_repos={"example/project"},
            amount_pattern=AMOUNT_RE,
        )
        self.assertNotIn("issue active in last 14d", result["career_reasons"])
        self.assertNotIn("recent active discussion", result["career_reasons"])
        self.assertIn("older inactive backlog penalty", result["career_reasons"])

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
