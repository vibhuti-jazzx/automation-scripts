#!/usr/bin/env python3
"""Validate SourceDocument and ClassifiedDocument page-count relationships."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from pypdf import PdfReader

from jazzx_api import ApiError, JazzXClient, parse_json_value, require_value, validate_uuid


DOCUMENT_TYPES = ("SourceDocument", "ClassifiedDocument")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare SourceDocument page counts with their ClassifiedDocument children."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--collection-id", "--collection")
    target.add_argument("--loan-number", "--loan")
    parser.add_argument("--gateway-url", "--base-url", default=os.getenv("JAZZX_GATEWAY_URL"))
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--original-pdf", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("document-page-count-reports"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--page-size", type=int, default=100)
    return parser


def entity_query(collection_id: str | None, entity_type: str) -> str:
    query = f"entity_type eq '{entity_type}'"
    return f"collection_id eq '{collection_id}' and {query}" if collection_id else query


def fetch_entities(
    client: JazzXClient,
    *,
    collection_id: str | None,
    entity_types: tuple[str, ...],
    page_size: int,
) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entity_type in entity_types:
        for item in client.iter_offset_pages(
            "/knowledge_hub/api/v1/reasoning/entities",
            params={"odata_query": entity_query(collection_id, entity_type)},
            page_size=page_size,
            offset_param="skip",
        ):
            entity_id = str(item.get("id") or "")
            if entity_id and entity_id not in seen:
                seen.add(entity_id)
                entities.append(item)
    return entities


def resolve_collection(client: JazzXClient, loan_number: str, page_size: int) -> str:
    matches: list[str] = []
    for entity in fetch_entities(
        client,
        collection_id=None,
        entity_types=("LoanProject",),
        page_size=page_size,
    ):
        value = parse_json_value(entity.get("json_value"))
        if str(value.get("external_loan_number") or "") == loan_number:
            collection_id = value.get("collection_id")
            if collection_id:
                matches.append(validate_uuid(str(collection_id), name="LoanProject collection_id"))
    unique = sorted(set(matches))
    if not unique:
        raise ValueError(f"no LoanProject found for loan number {loan_number!r}")
    if len(unique) > 1:
        raise ValueError(f"loan number {loan_number!r} maps to multiple collections: {', '.join(unique)}")
    return unique[0]


def parse_page_count(payload: Any) -> int:
    if isinstance(payload, dict):
        if payload.get("error"):
            raise ValueError(str(payload.get("text") or payload.get("message") or "tool returned an error"))
        if "page_count" in payload:
            count = payload["page_count"]
        else:
            text = payload.get("text")
            if isinstance(text, str):
                try:
                    nested = json.loads(text)
                except json.JSONDecodeError:
                    match = re.search(r"(?:page_count|pages?)\D+(\d+)", text, re.IGNORECASE)
                    if not match:
                        raise ValueError(f"unrecognized page-count response: {text[:300]!r}")
                    count = match.group(1)
                else:
                    return parse_page_count(nested)
            else:
                for key in ("data", "result", "output"):
                    if key in payload:
                        return parse_page_count(payload[key])
                raise ValueError("page-count response contains no count")
    else:
        count = payload
    try:
        result = int(count)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid page count {count!r}") from exc
    if result < 0:
        raise ValueError(f"page count cannot be negative: {result}")
    return result


def count_remote_pages(
    gateway_url: str,
    token: str,
    internal_file_id: str,
    timeout: float,
    retries: int,
) -> int:
    url = f"{gateway_url}/kernel/api/v1/agents/tools/test-run"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                json={"name": "count_pdf_page", "args": {"doc_id": internal_file_id}},
                timeout=timeout,
            )
            response.raise_for_status()
            return parse_page_count(response.json())
        except (requests.RequestException, requests.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"count_pdf_page failed for {internal_file_id}: {last_error}")


def fetch_page_counts(
    client: JazzXClient,
    file_ids: set[str],
    *,
    workers: int,
    timeout: float,
    retries: int,
) -> tuple[dict[str, int], dict[str, str]]:
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                count_remote_pages,
                client.gateway_url,
                client.token,
                file_id,
                timeout,
                retries,
            ): file_id
            for file_id in sorted(file_ids)
        }
        for future in as_completed(futures):
            file_id = futures[future]
            try:
                counts[file_id] = future.result()
            except RuntimeError as exc:
                errors[file_id] = str(exc)
    return counts, errors


def document_record(entity: dict[str, Any], counts: dict[str, int], errors: dict[str, str]) -> dict[str, Any]:
    value = parse_json_value(entity.get("json_value"))
    file_id = str(value.get("internal_file_id") or "")
    return {
        "id": str(entity.get("id") or ""),
        "entity_type": str(entity.get("entity_type") or ""),
        "name": str(value.get("name") or entity.get("name") or ""),
        "internal_file_id": file_id or None,
        "source_doc_id": value.get("source_doc_id"),
        "page_count": counts.get(file_id),
        "error": errors.get(file_id) if file_id else "missing internal_file_id",
    }


def build_report(
    collection_id: str,
    entities: list[dict[str, Any]],
    counts: dict[str, int],
    errors: dict[str, str],
    original_pdf: Path | None,
) -> dict[str, Any]:
    records = [document_record(entity, counts, errors) for entity in entities]
    sources = {record["id"]: record for record in records if record["entity_type"] == "SourceDocument"}
    grouped: dict[str | None, list[dict[str, Any]]] = {}
    for record in records:
        if record["entity_type"] == "ClassifiedDocument":
            grouped.setdefault(record["source_doc_id"], []).append(record)

    comparisons: list[dict[str, Any]] = []
    for source_id, source in sources.items():
        children = grouped.pop(source_id, [])
        child_counts = [child["page_count"] for child in children]
        complete = isinstance(source["page_count"], int) and all(isinstance(value, int) for value in child_counts)
        if not children:
            status = "NO_CLASSIFIED_CHILDREN"
        elif not complete:
            status = "INCOMPLETE"
        elif source["page_count"] == sum(child_counts):
            status = "VALID"
        else:
            status = "MISMATCH"
        comparisons.append(
            {
                "source": source,
                "classified_documents": children,
                "classified_page_total": sum(value for value in child_counts if isinstance(value, int)),
                "status": status,
            }
        )

    orphans = [child for remaining in grouped.values() for child in remaining]
    source_counts = [source["page_count"] for source in sources.values()]
    source_total = sum(value for value in source_counts if isinstance(value, int))
    classified_total = sum(item["classified_page_total"] for item in comparisons)
    source_vs_classified = (
        "NO_DATA"
        if not comparisons
        else "INCOMPLETE"
        if orphans or any(item["status"] in ("INCOMPLETE", "NO_CLASSIFIED_CHILDREN") for item in comparisons)
        else "VALID"
        if source_total == classified_total
        else "MISMATCH"
    )

    original_count: int | None = None
    original_status = "NOT_PROVIDED"
    if original_pdf:
        if not original_pdf.is_file():
            raise ValueError(f"original PDF not found: {original_pdf}")
        original_count = len(PdfReader(str(original_pdf)).pages)
        original_status = (
            "INCOMPLETE"
            if any(not isinstance(value, int) for value in source_counts)
            else "VALID"
            if original_count == source_total
            else "MISMATCH"
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collection_id": collection_id,
        "summary": {
            "source_documents": len(sources),
            "classified_documents": sum(len(item["classified_documents"]) for item in comparisons) + len(orphans),
            "source_page_total": source_total,
            "classified_page_total": classified_total,
            "source_vs_classified": source_vs_classified,
            "original_pdf": str(original_pdf) if original_pdf else None,
            "original_page_count": original_count,
            "original_vs_sources": original_status,
            "page_count_errors": len(errors) + sum(1 for record in records if not record["internal_file_id"]),
        },
        "comparisons": comparisons,
        "orphan_classified_documents": orphans,
        "page_count_errors": errors,
    }


def render_text(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"Collection: {report['collection_id']}",
        f"Source documents: {summary['source_documents']}",
        f"Classified documents: {summary['classified_documents']}",
        f"Source pages: {summary['source_page_total']}",
        f"Classified pages: {summary['classified_page_total']}",
        f"Source vs classified: {summary['source_vs_classified']}",
        f"Original vs sources: {summary['original_vs_sources']}",
        f"Page-count errors: {summary['page_count_errors']}",
        "",
    ]
    for item in report["comparisons"]:
        source = item["source"]
        lines.append(
            f"[{item['status']}] {source['name'] or source['id']}: "
            f"source={source['page_count']}, classified={item['classified_page_total']}"
        )
    if report["orphan_classified_documents"]:
        lines.append(f"Orphan classified documents: {len(report['orphan_classified_documents'])}")
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> int:
    if args.workers < 1 or args.workers > 16:
        raise ValueError("--workers must be between 1 and 16")
    if args.retries < 0 or args.retries > 10:
        raise ValueError("--retries must be between 0 and 10")
    gateway = require_value(args.gateway_url, name="--gateway-url", env_name="JAZZX_GATEWAY_URL")
    token = require_value(args.token, name="--token", env_name="JAZZX_TOKEN")
    client = JazzXClient(gateway, token, min(args.timeout, 60))
    collection_id = (
        validate_uuid(args.collection_id, name="--collection-id")
        if args.collection_id
        else resolve_collection(client, args.loan_number, args.page_size)
    )
    entities = fetch_entities(
        client,
        collection_id=collection_id,
        entity_types=DOCUMENT_TYPES,
        page_size=args.page_size,
    )
    file_ids = {
        str(parse_json_value(entity.get("json_value")).get("internal_file_id"))
        for entity in entities
        if parse_json_value(entity.get("json_value")).get("internal_file_id")
    }
    counts, errors = fetch_page_counts(
        client,
        file_ids,
        workers=args.workers,
        timeout=args.timeout,
        retries=args.retries,
    )
    report = build_report(collection_id, entities, counts, errors, args.original_pdf)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = args.output_dir / f"page_count_validation_{collection_id}_{stamp}"
    json_path = base.with_suffix(".json")
    text_path = base.with_suffix(".txt")
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    text_path.write_text(render_text(report), encoding="utf-8")
    print(render_text(report), end="")
    print(f"JSON report: {json_path}")
    print(f"Text report: {text_path}")
    bad_statuses = {"MISMATCH", "INCOMPLETE", "NO_DATA"}
    failed = report["summary"]["source_vs_classified"] in bad_statuses or report["summary"]["original_vs_sources"] in bad_statuses
    return 1 if failed else 0


def main() -> None:
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (ApiError, OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
