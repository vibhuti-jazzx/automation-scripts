from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sync_loan_to_pipeline import loan_values, pipeline_item_number, pipeline_items


class PipelineMappingTests(unittest.TestCase):
    def test_handles_empty_borrower_list(self) -> None:
        values = loan_values(
            {
                "loan": {"loanNumber": "100", "loanAmount": 50, "loanProduct": {}},
                "borrowers": [],
            }
        )
        self.assertEqual(values["number"], "100")
        self.assertEqual(values["borrower"], "Unknown Borrower")

    def test_maps_loan_core_shape(self) -> None:
        values = loan_values(
            {
                "loan_core": {"loan_number": "200", "loan_type": "Conv"},
                "loan_details": {"summary": {"borrowerName": "A Borrower"}},
            }
        )
        self.assertEqual(values["number"], "200")
        self.assertEqual(values["type"], "Conv Purchase")

    def test_rejects_invalid_pipeline_items(self) -> None:
        with self.assertRaises(ValueError):
            pipeline_items({"outputVariables": {"loan_pipeline_data": {"items": {}}}})

    def test_malformed_item_fields_do_not_crash_duplicate_check(self) -> None:
        self.assertEqual(pipeline_item_number({"fields": "invalid"}), "")


if __name__ == "__main__":
    unittest.main()
