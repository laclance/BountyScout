from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bountyscout.state as state_module
from bountyscout.state import (
    SeenState,
    SeenStateLoadError,
    SeenStateSaveError,
    load_seen_state,
    parse_seen_state,
    save_seen_state,
)


FIXED_REPORTED_AT = "2026-10-02T08:30:00Z"
FIXED_CHECKED_AT = "2026-10-02T10:00:00+02:00"
URL_A = "https://github.com/example/project/issues/1"
URL_B = "https://github.com/example/project/issues/2"


def current_document(
    *,
    version: object = 2,
    seen: object | None = None,
) -> dict[str, object]:
    return {
        "version": version,
        "seen": (
            {
                URL_A: {
                    "reported_at": FIXED_REPORTED_AT,
                    "last_checked_at": FIXED_CHECKED_AT,
                }
            }
            if seen is None
            else seen
        ),
    }


class SeenStateParsingTests(unittest.TestCase):
    def test_missing_file_is_empty_first_run_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = load_seen_state(Path(temp_dir) / "missing.json")

        self.assertEqual(loaded.urls(), set())

    def test_empty_legacy_list_loads(self) -> None:
        loaded = parse_seen_state([])
        self.assertEqual(loaded.urls(), set())

    def test_populated_legacy_list_preserves_all_urls_without_fake_timestamps(self) -> None:
        loaded = parse_seen_state([URL_B, URL_A])

        self.assertEqual(loaded.urls(), {URL_A, URL_B})
        self.assertEqual(loaded.record(URL_A), state_module.SeenEntry())
        self.assertEqual(loaded.record(URL_B), state_module.SeenEntry())

    def test_legacy_duplicate_urls_collapse_to_one_logical_record(self) -> None:
        loaded = parse_seen_state([URL_A, URL_A])
        self.assertEqual(loaded.urls(), {URL_A})

    def test_current_versioned_format_loads(self) -> None:
        loaded = parse_seen_state(current_document())

        self.assertTrue(loaded.contains(URL_A))
        self.assertEqual(
            loaded.record(URL_A),
            state_module.SeenEntry(
                reported_at=FIXED_REPORTED_AT,
                last_checked_at=FIXED_CHECKED_AT,
            ),
        )

    def test_unsupported_version_fails_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "Unsupported seen-state version"):
            parse_seen_state(current_document(version=3))

    def test_malformed_top_level_structures_fail_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "top level"):
            parse_seen_state("not-state")

        malformed = current_document()
        malformed["extra"] = True
        with self.assertRaisesRegex(SeenStateLoadError, "unexpected top-level fields"):
            parse_seen_state(malformed)

    def test_malformed_version_field_fails_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "missing the version"):
            parse_seen_state({"seen": {}})

        with self.assertRaisesRegex(SeenStateLoadError, "version must be an integer"):
            parse_seen_state(current_document(version=True))

    def test_malformed_seen_field_fails_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "'seen' field must be an object"):
            parse_seen_state(current_document(seen=[]))

    def test_malformed_entries_fail_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "must be an object"):
            parse_seen_state(current_document(seen={URL_A: []}))

        with self.assertRaisesRegex(SeenStateLoadError, "malformed fields"):
            parse_seen_state(current_document(seen={URL_A: {"reported_at": None}}))

    def test_malformed_url_values_fail_closed(self) -> None:
        with self.assertRaisesRegex(SeenStateLoadError, "URL keys"):
            parse_seen_state([42])

        with self.assertRaisesRegex(SeenStateLoadError, "URL keys"):
            parse_seen_state(
                current_document(seen={" ": {"reported_at": None, "last_checked_at": None}})
            )

        with self.assertRaisesRegex(SeenStateLoadError, "URL keys"):
            parse_seen_state(
                {
                    "version": 2,
                    "seen": {
                        42: {
                            "reported_at": None,
                            "last_checked_at": None,
                        }
                    },
                }
            )

    def test_malformed_timestamps_fail_closed(self) -> None:
        malformed_values: list[object] = [
            42,
            "",
            "not-a-date",
            "2026-10-02T10:00:00",
        ]
        for value in malformed_values:
            with self.subTest(value=value):
                with self.assertRaises(SeenStateLoadError):
                    parse_seen_state(
                        current_document(
                            seen={
                                URL_A: {
                                    "reported_at": value,
                                    "last_checked_at": None,
                                }
                            }
                        )
                    )

    def test_malformed_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(SeenStateLoadError, "Malformed JSON"):
                load_seen_state(path)

    def test_unreadable_existing_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(SeenStateLoadError, "Could not read seen-state"):
                load_seen_state(Path(temp_dir))


class SeenStateLogicTests(unittest.TestCase):
    def test_seen_and_unseen_membership(self) -> None:
        seen = SeenState.from_urls([URL_A])
        self.assertTrue(seen.contains(URL_A))
        self.assertIn(URL_A, seen)
        self.assertFalse(seen.contains(URL_B))
        self.assertNotIn(URL_B, seen)

    def test_mark_reported_adds_once_and_preserves_original_report_time(self) -> None:
        seen = SeenState()
        seen.mark_reported(URL_A, reported_at=FIXED_REPORTED_AT)
        seen.mark_reported(URL_A, reported_at=FIXED_CHECKED_AT)

        self.assertEqual(seen.urls(), {URL_A})
        self.assertEqual(
            seen.record(URL_A),
            state_module.SeenEntry(reported_at=FIXED_REPORTED_AT),
        )

    def test_mark_reported_many_handles_empty_and_populated_inputs(self) -> None:
        seen = SeenState()
        seen.mark_reported_many([])
        seen.mark_reported_many([URL_B, URL_A], reported_at=FIXED_REPORTED_AT)

        self.assertEqual(seen.urls(), {URL_A, URL_B})
        self.assertEqual(
            seen.record(URL_B), state_module.SeenEntry(reported_at=FIXED_REPORTED_AT)
        )
        self.assertIsNone(seen.record("https://github.com/example/project/issues/999"))

    def test_saving_migrated_legacy_state_emits_only_versioned_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            migrated = parse_seen_state([URL_B, URL_A])
            save_seen_state(migrated, path)
            saved: object = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            saved,
            {
                "version": 2,
                "seen": {
                    URL_A: {"reported_at": None, "last_checked_at": None},
                    URL_B: {"reported_at": None, "last_checked_at": None},
                },
            },
        )

    def test_save_reload_round_trip_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            original = SeenState()
            original.mark_reported(URL_A, reported_at=FIXED_REPORTED_AT)
            save_seen_state(original, path)
            loaded = load_seen_state(path)

        self.assertEqual(loaded.urls(), original.urls())
        self.assertEqual(loaded.record(URL_A), original.record(URL_A))

    def test_save_output_is_deterministic_readable_and_newline_terminated(self) -> None:
        expected = (
            "{\n"
            '  "version": 2,\n'
            '  "seen": {\n'
            f'    "{URL_A}": {{\n'
            '      "reported_at": null,\n'
            '      "last_checked_at": null\n'
            "    },\n"
            f'    "{URL_B}": {{\n'
            '      "reported_at": null,\n'
            '      "last_checked_at": null\n'
            "    }\n"
            "  }\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = Path(temp_dir) / "first.json"
            second_path = Path(temp_dir) / "second.json"
            save_seen_state(SeenState.from_urls([URL_B, URL_A]), first_path)
            save_seen_state(SeenState.from_urls([URL_A, URL_B]), second_path)

            first = first_path.read_text(encoding="utf-8")
            second = second_path.read_text(encoding="utf-8")

        self.assertEqual(first, expected)
        self.assertEqual(second, expected)

    def test_atomic_save_failure_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text("old-state\n", encoding="utf-8")
            with patch("bountyscout.state.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(SeenStateSaveError, "Could not save seen-state"):
                    save_seen_state(SeenState.from_urls([URL_A]), path)

            self.assertEqual(path.read_text(encoding="utf-8"), "old-state\n")


if __name__ == "__main__":
    unittest.main()
