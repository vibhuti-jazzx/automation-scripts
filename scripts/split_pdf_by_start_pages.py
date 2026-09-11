#!/usr/bin/env python3
"""Split a PDF using one-based start-page breakpoints."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Split a PDF at one-based document start pages. Page 1 may be included or omitted."
    )
    parser.add_argument("input_pdf", type=Path)
    parser.add_argument("start_pages", nargs="*", type=int, help="One-based start pages")
    parser.add_argument(
        "--start-pages-file",
        type=Path,
        help="Text/CSV file containing start pages separated by spaces, commas, or newlines",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--prefix", help="Output filename prefix; defaults to input stem")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def pages_from_file(path: Path) -> list[int]:
    if not path.is_file():
        raise ValueError(f"start-pages file not found: {path}")
    text = path.read_text(encoding="utf-8")
    tokens = [token for token in re.split(r"[\s,;]+", text.strip()) if token]
    try:
        return [int(token) for token in tokens]
    except ValueError as exc:
        raise ValueError(f"start-pages file contains a non-integer value: {exc}") from exc


def normalize_start_pages(values: list[int], total_pages: int) -> list[int]:
    starts = list(values)
    if not starts or starts[0] != 1:
        starts.insert(0, 1)
    if starts != sorted(starts):
        raise ValueError("start pages must be in ascending order")
    if len(starts) != len(set(starts)):
        raise ValueError("start pages cannot contain duplicates")
    invalid = [page for page in starts if page < 1 or page > total_pages]
    if invalid:
        raise ValueError(f"start pages outside PDF range 1-{total_pages}: {invalid}")
    return starts


def planned_outputs(input_pdf: Path, output_dir: Path, prefix: str, starts: list[int], total: int) -> list[tuple[Path, int, int]]:
    width = max(2, len(str(len(starts))))
    outputs: list[tuple[Path, int, int]] = []
    for index, start in enumerate(starts, start=1):
        end = starts[index] - 1 if index < len(starts) else total
        name = f"{prefix}_doc_{index:0{width}d}_pages_{start}-{end}.pdf"
        outputs.append((output_dir / name, start, end))
    return outputs


def split_pdf(
    input_pdf: Path,
    start_pages: list[int],
    *,
    output_dir: Path | None = None,
    prefix: str | None = None,
    overwrite: bool = False,
) -> list[Path]:
    if not input_pdf.is_file():
        raise ValueError(f"input PDF not found: {input_pdf}")
    try:
        reader = PdfReader(str(input_pdf))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ValueError("input PDF is password protected")
        total_pages = len(reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"could not read PDF: {exc}") from exc
    if total_pages == 0:
        raise ValueError("input PDF has no pages")

    starts = normalize_start_pages(start_pages, total_pages)
    destination = output_dir or input_pdf.with_name(f"{input_pdf.stem}_split")
    name_prefix = (prefix or input_pdf.stem).strip()
    if not name_prefix or Path(name_prefix).name != name_prefix:
        raise ValueError("prefix must be a plain filename component")
    outputs = planned_outputs(input_pdf, destination, name_prefix, starts, total_pages)
    conflicts = [path for path, _, _ in outputs if path.exists()]
    if conflicts and not overwrite:
        raise ValueError(f"output already exists: {conflicts[0]}; use --overwrite")
    destination.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for output_path, start, end in outputs:
        writer = PdfWriter()
        for page_number in range(start - 1, end):
            writer.add_page(reader.pages[page_number])
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{output_path.name}.", suffix=".tmp", dir=destination, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            writer.write(temporary)
        try:
            if len(PdfReader(str(temporary_path)).pages) != end - start + 1:
                raise ValueError(f"verification failed for {output_path.name}")
            temporary_path.replace(output_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        written.append(output_path)
    return written


def run(args: argparse.Namespace) -> int:
    starts = list(args.start_pages)
    if args.start_pages_file:
        starts.extend(pages_from_file(args.start_pages_file))
    if not starts:
        raise ValueError("provide at least one start page or --start-pages-file")
    outputs = split_pdf(
        args.input_pdf,
        starts,
        output_dir=args.output_dir,
        prefix=args.prefix,
        overwrite=args.overwrite,
    )
    for path in outputs:
        print(path)
    print(f"Created {len(outputs)} documents.")
    return 0


def main() -> None:
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
