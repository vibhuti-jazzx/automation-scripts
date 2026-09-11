#!/usr/bin/env python3
"""Preview or delete selected entities and documents from one collection."""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import requests

from jazzx_api import ApiError, JazzXClient, extract_items, require_value, validate_uuid


ENTITY_TYPES = (
    "Condition",
    "Finding",
    "LoanFindingsSummary",
    "FindingSet",
    "ClassifiedDocument",
    "SourceDocument",
    "NotificationLog",
    "MacerInvocationEvent",
    "PageCollection",
    "MergedConditionSet",
    "LoanDocumentScope",
)


@dataclass(frozen=True)
class Resource:
    kind: str
    resource_id: str
    label: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or delete entities and/or documents in a Knowledge Hub collection. "
        "Deletion is disabled unless --execute is supplied."
    )
    parser.add_argument("--collection-id", "--collection_id", required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--entity-types", "--entity_types", nargs="+", choices=ENTITY_TYPES)
    selection.add_argument("--all-entity-types", action="store_true")
    parser.add_argument("--include-documents", action="store_true")
    parser.add_argument("--documents-only", action="store_true")
    parser.add_argument("--gateway-url", "--base-url", default=os.getenv("JAZZX_GATEWAY_URL"))
    parser.add_argument("--token", "--auth-token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser


def entity_filter(collection_id: str, entity_type: str) -> str:
    # UUID/type choices are validated before interpolation.
    return f"collection_id eq '{collection_id}' and entity_type eq '{entity_type}'"


def collect_entities(
    client: JazzXClient, collection_id: str, entity_types: list[str], page_size: int
) -> list[Resource]:
    resources: list[Resource] = []
    seen: set[str] = set()
    for entity_type in entity_types:
        params = {"odata_query": entity_filter(collection_id, entity_type)}
        for item in client.iter_offset_pages(
            "/knowledge_hub/api/v1/reasoning/entities",
            params=params,
            page_size=page_size,
            offset_param="skip",
        ):
            resource_id = str(item.get("id") or "")
            if not resource_id or resource_id in seen:
                continue
            seen.add(resource_id)
            resources.append(
                Resource("entity", resource_id, str(item.get("name") or entity_type))
            )
    return resources


def collect_documents(client: JazzXClient, collection_id: str, page_size: int) -> list[Resource]:
    path = f"/knowledge_hub/api/v1/collections/{collection_id}/documents"
    resources: list[Resource] = []
    seen: set[str] = set()
    for item in client.iter_offset_pages(
        path,
        page_size=page_size,
        offset_param="skip",
        item_keys=("items", "data", "documents", "results"),
    ):
        resource_id = str(item.get("id") or item.get("document_id") or "")
        if not resource_id or resource_id in seen:
            continue
        seen.add(resource_id)
        resources.append(
            Resource(
                "document",
                resource_id,
                str(item.get("name") or item.get("file_name") or item.get("filename") or "document"),
            )
        )
    return resources


def print_preview(resources: list[Resource]) -> None:
    counts: dict[str, int] = {}
    for resource in resources:
        counts[resource.kind] = counts.get(resource.kind, 0) + 1
    print("Preview:")
    for kind in sorted(counts):
        print(f"  {kind}: {counts[kind]}")
    if not resources:
        print("  No matching resources found.")
        return
    for resource in resources[:25]:
        print(f"  - {resource.kind} {resource.resource_id}: {resource.label}")
    if len(resources) > 25:
        print(f"  ... and {len(resources) - 25} more")


def confirm(collection_id: str, count: int) -> None:
    if not sys.stdin.isatty():
        raise ValueError("interactive confirmation is unavailable; rerun with --yes")
    expected = f"purge {collection_id}"
    answer = input(f"Delete {count} resources? Type {expected!r}: ").strip()
    if answer != expected:
        raise KeyboardInterrupt("confirmation did not match")


def delete_one(base_url: str, token: str, timeout: float, resource: Resource) -> tuple[Resource, str | None]:
    if resource.kind == "entity":
        path = f"/knowledge_hub/api/v1/reasoning/entities/{resource.resource_id}"
    else:
        path = f"/knowledge_hub/api/v1/collections/documents/{resource.resource_id}"
    try:
        response = requests.delete(
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return resource, str(exc)
    if response.status_code in (200, 202, 204, 404):
        return resource, None
    return resource, f"HTTP {response.status_code}: {response.text.strip()[:500]}"


def run(args: argparse.Namespace) -> int:
    if args.documents_only and (args.entity_types or args.all_entity_types):
        raise ValueError("--documents-only cannot be combined with an entity selection")
    if not args.documents_only and not args.entity_types and not args.all_entity_types:
        raise ValueError("select --entity-types, --all-entity-types, or --documents-only")
    if args.workers < 1 or args.workers > 32:
        raise ValueError("--workers must be between 1 and 32")

    collection_id = validate_uuid(args.collection_id, name="--collection-id")
    gateway = require_value(args.gateway_url, name="--gateway-url", env_name="JAZZX_GATEWAY_URL")
    token = require_value(args.token, name="--token", env_name="JAZZX_TOKEN")
    client = JazzXClient(gateway, token, args.timeout)

    entity_types = [] if args.documents_only else list(ENTITY_TYPES if args.all_entity_types else args.entity_types)
    resources = collect_entities(client, collection_id, entity_types, args.page_size)
    if args.include_documents or args.documents_only:
        resources.extend(collect_documents(client, collection_id, args.page_size))
    print_preview(resources)

    if not args.execute:
        print("Dry run only. Add --execute to delete the listed resources.")
        return 0
    if not resources:
        return 0
    if not args.yes:
        confirm(collection_id, len(resources))

    failures: list[tuple[Resource, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(delete_one, client.gateway_url, client.token, args.timeout, resource)
            for resource in resources
        ]
        for future in as_completed(futures):
            resource, error = future.result()
            if error:
                failures.append((resource, error))

    deleted = len(resources) - len(failures)
    print(f"Deleted/already absent: {deleted}; failed: {len(failures)}")
    for resource, error in failures:
        print(f"ERROR: {resource.kind} {resource.resource_id}: {error}", file=sys.stderr)
    return 1 if failures else 0


def main() -> None:
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (ApiError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt as exc:
        print(f"Cancelled: {exc}", file=sys.stderr)
        raise SystemExit(130) from exc


if __name__ == "__main__":
    main()
