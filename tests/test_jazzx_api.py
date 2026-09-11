from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from jazzx_api import extract_items, normalize_gateway_url, normalize_token, parse_json_value, validate_uuid


class JazzXApiTests(unittest.TestCase):
    def test_normalizes_gateway_and_bearer_token(self) -> None:
        self.assertEqual(normalize_gateway_url("https://example.test/"), "https://example.test")
        self.assertEqual(normalize_token("Bearer abc"), "abc")

    def test_rejects_invalid_url_and_uuid(self) -> None:
        with self.assertRaises(ValueError):
            normalize_gateway_url("example.test")
        with self.assertRaises(ValueError):
            validate_uuid("loan-name", name="project")

    def test_extracts_common_response_envelopes(self) -> None:
        self.assertEqual(extract_items([{"id": "1"}]), [{"id": "1"}])
        self.assertEqual(extract_items({"data": {"items": [{"id": "2"}]}}), [{"id": "2"}])

    def test_parses_json_value_without_throwing(self) -> None:
        self.assertEqual(parse_json_value('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json_value("not-json"), {})


if __name__ == "__main__":
    unittest.main()
