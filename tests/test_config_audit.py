"""Pure tests for the standalone configuration UUID audit."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import config_audit as ca  # noqa: E402


class FindUUIDs(unittest.TestCase):
    def test_walks_nested_dicts_and_lists(self):
        value = {
            "ontology": "00000000-0000-0000-0000-000000000001",
            "nested": [{"collection": "00000000-0000-0000-0000-000000000002"}],
            "not_an_id": "loan-123",
        }
        self.assertEqual(
            ca.find_uuids(value),
            [
                ("ontology", "00000000-0000-0000-0000-000000000001"),
                ("nested[0].collection", "00000000-0000-0000-0000-000000000002"),
            ],
        )

    def test_does_not_match_partial_uuid_strings(self):
        self.assertEqual(ca.find_uuids({"id": "prefix-00000000-0000-0000-0000-000000000001"}), [])


def row(kind: str, name: str, detail: str = "", rid: str = "id") -> dict:
    return {"id": rid, "resolved_as": kind, "name": name, "detail": detail}


def report(gateway: str, rows: dict[str, dict]) -> dict:
    return {
        "gateway_url": gateway,
        "config_name": "APP_CONFIG",
        "references": [{**value, "config_key": key} for key, value in rows.items()],
    }


class Comparisons(unittest.TestCase):
    def test_different_ids_match_when_they_resolve_to_same_object(self):
        compared = ca.compare_reports(
            report("https://a", {"ontology_id": row("ontology", "docs", rid="a")}),
            report("https://b", {"ontology_id": row("ontology", "docs", rid="b")}),
            "staging",
            "poc",
        )
        self.assertEqual(compared["match_count"], 1)
        self.assertEqual(compared["problem_count"], 0)
        self.assertFalse(compared["rows"][0]["same_id"])

    def test_name_mismatch_is_reported(self):
        compared = ca.compare_reports(
            report("https://a", {"ontology_id": row("ontology", "docs_v1")}),
            report("https://b", {"ontology_id": row("ontology", "docs_v2")}),
            "staging",
            "poc",
        )
        self.assertEqual(compared["problem_count"], 1)
        self.assertEqual(compared["rows"][0]["differs_on"], ["name"])

    def test_missing_key_is_reported(self):
        compared = ca.compare_reports(
            report("https://a", {"only_here": row("collection", "loans")}),
            report("https://b", {}),
            "staging",
            "poc",
        )
        self.assertEqual(compared["rows"][0]["verdict"], "MISSING_IN_POC")


class Markdown(unittest.TestCase):
    def test_report_contains_each_reference_and_summary(self):
        value = {
            "gateway_url": "https://poc-gw.example",
            "config_name": "APP_CONFIG",
            "config_id": "config-1",
            "reference_count": 1,
            "unresolved_count": 0,
            "counts_by_kind": {"ontology": 1},
            "references": [
                {
                    "config_key": "docs_ontology_id",
                    "id": "00000000-0000-0000-0000-000000000001",
                    "resolved_as": "ontology",
                    "name": "docs",
                    "detail": "",
                }
            ],
        }
        rendered = ca.render_markdown(value, "poc")
        self.assertIn("# Configuration UUID audit — poc", rendered)
        self.assertIn("| docs_ontology_id |", rendered)
        self.assertIn("Summary: ontology: 1.", rendered)


if __name__ == "__main__":
    unittest.main()
