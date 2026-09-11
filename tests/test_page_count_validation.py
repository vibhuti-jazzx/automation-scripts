from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from validate_document_page_counts import build_report, parse_page_count


class PageCountValidationTests(unittest.TestCase):
    def test_parses_supported_tool_responses(self) -> None:
        self.assertEqual(parse_page_count({"text": '{"page_count": 12}'}), 12)
        self.assertEqual(parse_page_count({"text": "Total pages: 9"}), 9)
        self.assertEqual(parse_page_count({"data": {"page_count": 3}}), 3)

    def test_builds_valid_source_to_classified_comparison(self) -> None:
        entities = [
            {
                "id": "source-1",
                "entity_type": "SourceDocument",
                "json_value": {"name": "source", "internal_file_id": "file-source"},
            },
            {
                "id": "child-1",
                "entity_type": "ClassifiedDocument",
                "json_value": {
                    "name": "child 1",
                    "internal_file_id": "file-1",
                    "source_doc_id": "source-1",
                },
            },
            {
                "id": "child-2",
                "entity_type": "ClassifiedDocument",
                "json_value": {
                    "name": "child 2",
                    "internal_file_id": "file-2",
                    "source_doc_id": "source-1",
                },
            },
        ]
        report = build_report(
            "00000000-0000-0000-0000-000000000000",
            entities,
            {"file-source": 5, "file-1": 2, "file-2": 3},
            {},
            None,
        )
        self.assertEqual(report["summary"]["source_vs_classified"], "VALID")
        self.assertEqual(report["comparisons"][0]["status"], "VALID")


if __name__ == "__main__":
    unittest.main()
