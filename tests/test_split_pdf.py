from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from split_pdf_by_start_pages import normalize_start_pages, split_pdf


class SplitPdfTests(unittest.TestCase):
    def test_normalizes_and_validates_breakpoints(self) -> None:
        self.assertEqual(normalize_start_pages([3, 5], 7), [1, 3, 5])
        with self.assertRaises(ValueError):
            normalize_start_pages([1, 5, 3], 7)
        with self.assertRaises(ValueError):
            normalize_start_pages([1, 3, 3], 7)
        with self.assertRaises(ValueError):
            normalize_start_pages([1, 8], 7)

    def test_splits_and_protects_existing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            writer = PdfWriter()
            for _ in range(6):
                writer.add_blank_page(width=100, height=100)
            with source.open("wb") as stream:
                writer.write(stream)

            outputs = split_pdf(source, [1, 3, 6])
            self.assertEqual([len(PdfReader(str(path)).pages) for path in outputs], [2, 3, 1])
            with self.assertRaises(ValueError):
                split_pdf(source, [1, 3, 6])
            overwritten = split_pdf(source, [1, 3, 6], overwrite=True)
            self.assertEqual(len(overwritten), 3)


if __name__ == "__main__":
    unittest.main()
