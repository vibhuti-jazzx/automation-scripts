#!/usr/bin/env python3
"""Resolve every UUID inside a global config against the instance it points at.

APP_CONFIG holds ids -- findings_ontology_id, docs_ontology_id, collection ids and
so on -- as bare strings. Nothing validates them. A stale or mistyped id fails far
downstream, as a process that finds no data rather than an error at startup.

This reads the config, pulls out every UUID, and asks the instance what each one
actually is: an ontology, a collection, an entity, or nothing at all. The last
case is the one worth finding.

Read-only: every request is a GET.

    export JAZZX_GATEWAY_URL="https://<your-gateway-host>"
    export JAZZX_TOKEN="<bearer token>"

    python3 scripts/audit_app_config_ids.py
    python3 scripts/audit_app_config_ids.py --config-name APP_CONFIG --output audit.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jazzx_api import (  # noqa: E402
    ApiError,
    JazzXClient,
    extract_items,
    normalize_gateway_url,
    parse_json_value,
    require_value,
)

# The trailing slash is required: without it the service answers
# {"status": false, "message": "Endpoint not found."}. Every working caller in the
# monorepo (deploy.py, read_kh_config.py) sends it. The by-id form has no slash.
GLOBAL_CONFIG_PATH = "/knowledge_hub/api/v1/global-config/"
ENTITIES_PATH = "/knowledge_hub/api/v1/reasoning/entities"
# NOTE: ontologies live at /reasoning/ontologies, not /reasoning/entities.
ONTOLOGIES_PATH = "/knowledge_hub/api/v1/reasoning/ontologies"
COLLECTIONS_PATH = "/knowledge_hub/api/v1/collections"

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
PAGE_LIMIT = 500


def find_uuids(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Every UUID-looking string in a nested structure, with its key path.

    Walks the whole value rather than reading a fixed list of keys, so an id added
    to the config later is audited without this script needing to change.
    """
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(find_uuids(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(find_uuids(item, f"{path}[{index}]"))
    elif isinstance(value, str) and UUID_RE.match(value.strip()):
        found.append((path, value.strip()))
    return found


def fetch_all(client: JazzXClient, path: str, **params: Any) -> list[dict[str, Any]]:
    """Page a list endpoint until it stops returning full pages."""
    items: list[dict[str, Any]] = []
    skip = 0
    while True:
        payload = client.request(
            "GET", path, params={"skip": skip, "limit": PAGE_LIMIT, **params}
        ).json()
        page = extract_items(payload)
        items.extend(page)
        if len(page) < PAGE_LIMIT:
            return items
        skip += PAGE_LIMIT
        if skip > 100_000:
            return items


def exists(client: JazzXClient, path: str) -> dict[str, Any] | None:
    """GET one resource; None when the instance says it is not there."""
    try:
        response = client.request("GET", path, expected=(200, 404))
    except ApiError:
        return None
    if response.status_code == 404:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--gateway-url", default=os.getenv("JAZZX_GATEWAY_URL", ""))
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--config-name", default="APP_CONFIG",
                        help="Global config to audit (default: APP_CONFIG)")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", type=Path, help="Write the full report as JSON")
    args = parser.parse_args()

    client = JazzXClient(
        gateway_url=normalize_gateway_url(
            require_value(args.gateway_url, name="--gateway-url", env_name="JAZZX_GATEWAY_URL")
        ),
        token=require_value(args.token, name="--token", env_name="JAZZX_TOKEN"),
        timeout=args.timeout,
    )

    # 1. locate the config by name. The endpoint filters server side on ?name=,
    #    and answers with a plain JSON list.
    configs = extract_items(
        client.request("GET", GLOBAL_CONFIG_PATH, params={"name": args.config_name}).json()
    )
    match = next(
        (c for c in configs if str(c.get("name") or c.get("key") or "") == args.config_name),
        None,
    )
    if match is None:
        # Fall back to an unfiltered listing so the error can name what does exist.
        every = extract_items(client.request("GET", GLOBAL_CONFIG_PATH).json())
        names = sorted({str(c.get("name") or c.get("key") or "?") for c in every})
        raise ApiError(
            f"No global config named {args.config_name!r}. Found: {', '.join(names) or '<none>'}"
        )
    config_id = str(match.get("id") or "")
    print(f"{args.config_name}: id {config_id}", file=sys.stderr)

    # 2. the listing already carries `value`; re-read by id only if it did not.
    raw_value = match.get("value")
    if not raw_value and config_id:
        detail = exists(client, f"{GLOBAL_CONFIG_PATH.rstrip('/')}/{config_id}") or {}
        raw_value = detail.get("value", detail.get("config", detail))
    value = parse_json_value(raw_value) or (raw_value if isinstance(raw_value, dict) else {})

    # 3. every UUID in it, wherever it sits
    references = find_uuids(value)
    print(f"UUID references found: {len(references)}", file=sys.stderr)
    if not references:
        print("Nothing to resolve -- the config holds no UUIDs.", file=sys.stderr)
        return 0

    # 4. one ontology listing serves every lookup; collections and entities are
    #    fetched per id, since listing every entity on the instance is far dearer.
    print("Reading ontologies ...", file=sys.stderr)
    ontologies = {str(o.get("id")): o for o in fetch_all(client, ONTOLOGIES_PATH) if o.get("id")}
    print(f"  {len(ontologies)} ontologies", file=sys.stderr)

    rows: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for key_path, candidate in references:
        if candidate in seen:
            rows.append({**seen[candidate], "config_key": key_path})
            continue
        kind, name, detail_note = "UNRESOLVED", "", "not an ontology, collection or entity"
        if candidate in ontologies:
            kind = "ontology"
            name = str(ontologies[candidate].get("name") or "")
            detail_note = ""
        else:
            collection = exists(client, f"{COLLECTIONS_PATH}/{candidate}")
            if collection is not None:
                kind = "collection"
                name = str(collection.get("name") or "")
                detail_note = ""
            else:
                entity = exists(client, f"{ENTITIES_PATH}/{candidate}")
                if entity is not None:
                    kind = "entity"
                    name = str(entity.get("name") or "")
                    detail_note = str(entity.get("entity_type") or "")
        record = {"id": candidate, "resolved_as": kind, "name": name, "detail": detail_note}
        seen[candidate] = record
        rows.append({**record, "config_key": key_path})

    # 5. report
    width = max((len(r["config_key"]) for r in rows), default=20) + 2
    print("\n" + "=" * (width + 74))
    print(f"APP CONFIG ID AUDIT  —  {args.config_name}  @  {client.gateway_url}")
    print("=" * (width + 74))
    print(f"\n  {'config key':<{width}}{'id':<38}{'resolved as':<13}name")
    print("  " + "-" * (width + 72))
    for row in rows:
        label = row["name"] or row["detail"]
        print(f"  {row['config_key']:<{width}}{row['id']:<38}{row['resolved_as']:<13}{label[:34]}")

    unresolved = [r for r in rows if r["resolved_as"] == "UNRESOLVED"]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["resolved_as"]] = counts.get(row["resolved_as"], 0) + 1
    print("\n  " + "  ".join(f"{kind}: {count}" for kind, count in sorted(counts.items())))
    if unresolved:
        print(f"\n  {len(unresolved)} id(s) matched NOTHING on this instance:")
        for row in unresolved:
            print(f"    {row['config_key']} = {row['id']}")
        print("  A config id that resolves to nothing fails silently downstream —")
        print("  processes read it, find no data, and report success with empty results.")
    else:
        print("\n  Every id in the config resolves to something on this instance.")
    print("=" * (width + 74) + "\n")

    report = {
        "gateway_url": client.gateway_url,
        "config_name": args.config_name,
        "config_id": config_id,
        "reference_count": len(rows),
        "counts_by_kind": counts,
        "unresolved_count": len(unresolved),
        "references": rows,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    return 1 if unresolved else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApiError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
