#!/usr/bin/env python3
"""Copy Reasoner entities from one Knowledge Hub collection to another.

The source and target may live on different JazzX instances. Ontology IDs
differ between instances, so each source entity's ontology is resolved by
name (preferring the same version) on the target. Entities that already
exist in the target collection with the same name and entity type are
skipped. Nothing is written unless --execute is supplied.

Example:

    export JAZZX_SOURCE_TOKEN="<poc3 JWT>"
    export JAZZX_TARGET_TOKEN="<poc4 JWT>"
    python3 scripts/copy_collection_entities.py \
      --source-gateway-url https://poc3-gw.jazzx.co \
      --source-collection-id 59ec3b8f-131b-4b95-842e-51c2ad1eb6e8 \
      --target-gateway-url https://poc4-gw.jazzx.co \
      --target-collection-id f4f91cbf-4e75-4b3c-9c1d-c320f4451d8b
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jazzx_api import ApiError, JazzXClient, extract_items, require_value, validate_uuid  # noqa: E402


ENTITIES_PATH = "/knowledge_hub/api/v1/reasoning/entities"
ONTOLOGIES_PATH = "/knowledge_hub/api/v1/reasoning/ontologies"
PAGE_LIMIT = 100
READ_ONLY_FIELDS = {"id", "created_at", "updated_at", "created_by", "updated_by", "collection_id"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source-gateway-url", required=True)
    parser.add_argument("--source-token", help="defaults to $JAZZX_SOURCE_TOKEN")
    parser.add_argument("--source-collection-id", required=True)
    parser.add_argument("--target-gateway-url", required=True)
    parser.add_argument("--target-token", help="defaults to $JAZZX_TARGET_TOKEN")
    parser.add_argument("--target-collection-id", required=True)
    parser.add_argument("--entity-ids", nargs="+", help="copy only these source entity IDs")
    parser.add_argument("--entity-types", nargs="+", help="copy only these entity types")
    parser.add_argument("--save-source", help="write the fetched source entities to this JSON file")
    parser.add_argument("--execute", action="store_true", help="create the entities (default is a dry run)")
    return parser.parse_args()


def fetch_all(client: JazzXClient, path: str, **params: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    skip = 0
    while True:
        payload = client.request_json("GET", path, params={"skip": skip, "limit": PAGE_LIMIT, **params})
        page = extract_items(payload)
        items.extend(page)
        if len(page) < PAGE_LIMIT or skip > 100_000:
            return items
        skip += PAGE_LIMIT


def collection_entities(client: JazzXClient, collection_id: str) -> list[dict[str, Any]]:
    return fetch_all(client, ENTITIES_PATH, odata_query=f"collection_id eq '{collection_id}'")


def map_ontologies(
    source: JazzXClient, target: JazzXClient, ontology_ids: set[str]
) -> tuple[dict[str, str], list[str]]:
    """Map each source ontology ID to a target ontology ID by name."""
    source_by_id = {str(o.get("id")): o for o in fetch_all(source, ONTOLOGIES_PATH)}
    target_all = fetch_all(target, ONTOLOGIES_PATH)
    mapping: dict[str, str] = {}
    problems: list[str] = []
    for ontology_id in sorted(ontology_ids):
        src = source_by_id.get(ontology_id)
        if not src:
            problems.append(f"source ontology {ontology_id} not found")
            continue
        name = src.get("name")
        candidates = [o for o in target_all if o.get("name") == name]
        same_version = [o for o in candidates if o.get("version") == src.get("version")]
        chosen = same_version or candidates
        if not chosen:
            problems.append(f"ontology {name!r} (version {src.get('version')}) does not exist on target")
            continue
        if not same_version:
            print(
                f"  note: ontology {name!r} version {src.get('version')} not on target; "
                f"using version {chosen[-1].get('version')}"
            )
        mapping[ontology_id] = str(chosen[-1]["id"])
    return mapping, problems


def replace_strings(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {k: replace_strings(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_strings(v, old, new) for v in value]
    if value == old:
        return new
    return value


def build_payload(entity: dict[str, Any], args: argparse.Namespace, ontology_map: dict[str, str]) -> dict[str, Any]:
    payload = {k: deepcopy(v) for k, v in entity.items() if k not in READ_ONLY_FIELDS}
    payload["collection_id"] = args.target_collection_id
    if entity.get("ontology_id"):
        payload["ontology_id"] = ontology_map[str(entity["ontology_id"])]
    if "json_value" in payload:
        payload["json_value"] = replace_strings(
            payload["json_value"], args.source_collection_id, args.target_collection_id
        )
    return payload


def main() -> int:
    args = parse_args()
    args.source_collection_id = validate_uuid(args.source_collection_id, name="source collection ID")
    args.target_collection_id = validate_uuid(args.target_collection_id, name="target collection ID")
    source = JazzXClient(
        args.source_gateway_url,
        require_value(args.source_token, name="source token", env_name="JAZZX_SOURCE_TOKEN"),
    )
    target = JazzXClient(
        args.target_gateway_url,
        require_value(args.target_token, name="target token", env_name="JAZZX_TARGET_TOKEN"),
    )

    entities = collection_entities(source, args.source_collection_id)
    if args.entity_ids:
        wanted = {validate_uuid(i, name="entity ID") for i in args.entity_ids}
        entities = [e for e in entities if str(e.get("id")) in wanted]
    if args.entity_types:
        entities = [e for e in entities if e.get("entity_type") in set(args.entity_types)]
    if not entities:
        print("No matching entities in the source collection.")
        return 1
    if args.save_source:
        Path(args.save_source).write_text(json.dumps(entities, indent=2))
        print(f"Saved {len(entities)} source entities to {args.save_source}")

    ontology_ids = {str(e["ontology_id"]) for e in entities if e.get("ontology_id")}
    ontology_map, problems = map_ontologies(source, target, ontology_ids)
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}")
        return 1

    existing = {
        (e.get("name"), e.get("entity_type")) for e in collection_entities(target, args.target_collection_id)
    }

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"[{mode}] {len(entities)} source entities -> {args.target_gateway_url} collection {args.target_collection_id}")
    failures = 0
    for entity in entities:
        label = f"{entity.get('entity_type')} {entity.get('name')!r} ({entity.get('id')})"
        if (entity.get("name"), entity.get("entity_type")) in existing:
            print(f"  skip   {label}: already exists in target")
            continue
        payload = build_payload(entity, args, ontology_map)
        if not args.execute:
            print(f"  create {label}")
            continue
        try:
            created = target.request_json("POST", ENTITIES_PATH, json_body=payload, expected=(200, 201))
        except ApiError as exc:
            failures += 1
            print(f"  FAILED {label}: {exc}")
            continue
        new_id = created.get("id") if isinstance(created, dict) else None
        print(f"  created {label} -> {new_id}")
    if not args.execute:
        print("Dry run only; re-run with --execute to create.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ApiError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
