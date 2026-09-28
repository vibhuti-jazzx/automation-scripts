#!/usr/bin/env python3
"""Resolve every UUID inside a global config against the instance it points at.

APP_CONFIG holds ids -- findings_ontology_id, docs_ontology_id, collection ids and
so on -- as bare strings. Nothing validates them. A stale or mistyped id fails far
downstream, as a process that finds no data rather than an error at startup.

This reads the config, pulls out every UUID, and asks the instance what each one
actually is: an ontology, a collection, an entity, or nothing at all. The last
case is the one worth finding.

Read-only: every request is a GET.

Given two instances it audits both and adds a third report comparing them. The
ids are EXPECTED to differ -- each instance mints its own -- so the comparison
ignores them and asserts the rest: the same config key must resolve to the same
kind, the same name and the same detail on both sides. A key that names
"finding_ontology" on one instance and something else on the other is drift,
and so is one that resolves on one and not the other.

    export JAZZX_TOKEN="<bearer token>"          # instance A
    export JAZZX_TOKEN_B="<bearer token>"        # instance B, when comparing

    # one instance
    python3 scripts/audit_app_config_ids.py --gateway-url https://<host>

    # two instances, three reports
    python3 scripts/audit_app_config_ids.py \
        --gateway-url   https://<host-a> --label uat \
        --gateway-url-b https://<host-b> --label-b poc2 \
        --output-dir config-audit/
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


def audit_instance(client: JazzXClient, config_name: str) -> dict[str, Any]:
    """Resolve every UUID in one instance's config. Returns the report dict."""
    configs = extract_items(
        client.request("GET", GLOBAL_CONFIG_PATH, params={"name": config_name}).json()
    )
    match = next(
        (c for c in configs if str(c.get("name") or c.get("key") or "") == config_name), None
    )
    if match is None:
        every = extract_items(client.request("GET", GLOBAL_CONFIG_PATH).json())
        names = sorted({str(c.get("name") or c.get("key") or "?") for c in every})
        raise ApiError(
            f"No global config named {config_name!r} on {client.gateway_url}. "
            f"Found: {', '.join(names) or '<none>'}"
        )
    config_id = str(match.get("id") or "")

    raw_value = match.get("value")
    if not raw_value and config_id:
        detail = exists(client, f"{GLOBAL_CONFIG_PATH.rstrip('/')}/{config_id}") or {}
        raw_value = detail.get("value", detail.get("config", detail))
    value = parse_json_value(raw_value) or (raw_value if isinstance(raw_value, dict) else {})

    references = find_uuids(value)
    print(f"  {client.gateway_url}: config {config_id}, {len(references)} UUID references",
          file=sys.stderr)

    ontologies = {str(o.get("id")): o for o in fetch_all(client, ONTOLOGIES_PATH) if o.get("id")}
    rows: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for key_path, candidate in references:
        if candidate not in seen:
            kind, name, note = "UNRESOLVED", "", "not an ontology, collection or entity"
            if candidate in ontologies:
                kind, name, note = "ontology", str(ontologies[candidate].get("name") or ""), ""
            else:
                collection = exists(client, f"{COLLECTIONS_PATH}/{candidate}")
                if collection is not None:
                    kind, name, note = "collection", str(collection.get("name") or ""), ""
                else:
                    entity = exists(client, f"{ENTITIES_PATH}/{candidate}")
                    if entity is not None:
                        kind = "entity"
                        name = str(entity.get("name") or "")
                        note = str(entity.get("entity_type") or "")
            seen[candidate] = {"id": candidate, "resolved_as": kind, "name": name, "detail": note}
        rows.append({**seen[candidate], "config_key": key_path})

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["resolved_as"]] = counts.get(row["resolved_as"], 0) + 1
    unresolved = [r for r in rows if r["resolved_as"] == "UNRESOLVED"]
    return {
        "gateway_url": client.gateway_url,
        "config_name": config_name,
        "config_id": config_id,
        "reference_count": len(rows),
        "counts_by_kind": counts,
        "unresolved_count": len(unresolved),
        "references": rows,
    }


def print_audit(report: dict[str, Any], label: str) -> None:
    rows = report["references"]
    width = max((len(r["config_key"]) for r in rows), default=20) + 2
    print("\n" + "=" * (width + 74))
    print(f"APP CONFIG ID AUDIT  —  {label}  —  {report['config_name']} @ {report['gateway_url']}")
    print("=" * (width + 74))
    print(f"\n  {'config key':<{width}}{'id':<38}{'resolved as':<13}name")
    print("  " + "-" * (width + 72))
    for row in rows:
        shown = row["name"] or row["detail"]
        print(f"  {row['config_key']:<{width}}{row['id']:<38}{row['resolved_as']:<13}{shown[:34]}")
    print("\n  " + "  ".join(f"{k}: {v}" for k, v in sorted(report["counts_by_kind"].items())))
    unresolved = [r for r in rows if r["resolved_as"] == "UNRESOLVED"]
    if unresolved:
        print(f"\n  {len(unresolved)} id(s) matched NOTHING on this instance:")
        for row in unresolved:
            print(f"    {row['config_key']} = {row['id']}")
    else:
        print("\n  Every id resolves to something on this instance.")
    print("=" * (width + 74))


COMPARED_FIELDS = ("resolved_as", "name", "detail")


def compare_reports(a: dict[str, Any], b: dict[str, Any],
                    label_a: str, label_b: str) -> dict[str, Any]:
    """Compare two audits by config key, ignoring ids.

    Each instance mints its own UUIDs, so a differing id is expected and says
    nothing. What must agree is what the id POINTS AT: the same kind of object,
    with the same name and detail. Divergence there means the two instances are
    configured differently however identical the config file looks.
    """
    by_key_a = {r["config_key"]: r for r in a["references"]}
    by_key_b = {r["config_key"]: r for r in b["references"]}
    rows: list[dict[str, Any]] = []
    for key in sorted(set(by_key_a) | set(by_key_b)):
        ra, rb = by_key_a.get(key), by_key_b.get(key)
        if ra is None or rb is None:
            rows.append({
                "config_key": key,
                "verdict": f"MISSING_IN_{label_b.upper()}" if rb is None
                           else f"MISSING_IN_{label_a.upper()}",
                "differs_on": ["config_key"],
                label_a: ra, label_b: rb,
            })
            continue
        differs = [f for f in COMPARED_FIELDS if (ra.get(f) or "") != (rb.get(f) or "")]
        rows.append({
            "config_key": key,
            "verdict": "MATCH" if not differs else "MISMATCH",
            "differs_on": differs,
            "same_id": ra["id"] == rb["id"],
            label_a: ra, label_b: rb,
        })
    problems = [r for r in rows if r["verdict"] != "MATCH"]
    return {
        "label_a": label_a, "label_b": label_b,
        "gateway_a": a["gateway_url"], "gateway_b": b["gateway_url"],
        "config_name": a["config_name"],
        "compared_fields": list(COMPARED_FIELDS),
        "key_count": len(rows),
        "match_count": len(rows) - len(problems),
        "problem_count": len(problems),
        "shared_id_count": sum(1 for r in rows if r.get("same_id")),
        "rows": rows,
    }


def print_comparison(cmp: dict[str, Any]) -> None:
    a, b = cmp["label_a"], cmp["label_b"]
    rows = cmp["rows"]
    width = max((len(r["config_key"]) for r in rows), default=20) + 2
    print("\n" + "=" * (width + 66))
    print(f"CROSS-INSTANCE COMPARISON  —  {a}  vs  {b}")
    print(f"comparing {', '.join(cmp['compared_fields'])}; ids are expected to differ")
    print("=" * (width + 66))
    print(f"\n  {'config key':<{width}}{'verdict':<12}{'differs on':<24}{a} / {b}")
    print("  " + "-" * (width + 64))
    for row in rows:
        ra, rb = row.get(a), row.get(b)
        if row["verdict"] == "MATCH":
            shown = (ra["name"] or ra["detail"])[:30]
        else:
            shown = f"{(ra or {}).get('name') or (ra or {}).get('resolved_as') or '-'}" \
                    f"  /  {(rb or {}).get('name') or (rb or {}).get('resolved_as') or '-'}"
        print(f"  {row['config_key']:<{width}}{row['verdict']:<12}"
              f"{','.join(row['differs_on']) or '-':<24}{shown[:44]}")
    print(f"\n  {cmp['match_count']} match, {cmp['problem_count']} differ, "
          f"of {cmp['key_count']} config keys")
    if cmp["shared_id_count"]:
        print(f"  {cmp['shared_id_count']} key(s) share the SAME id across instances — "
              f"expected only if the instances share storage.")
    if cmp["problem_count"]:
        print("\n  Differences mean the two instances point at different things behind")
        print("  identical config keys. Whatever reads this config behaves differently")
        print("  on each, with nothing in the config itself to show why.")
    else:
        print("\n  Both instances resolve every config key to the same kind, name and detail.")
    print("=" * (width + 66) + "\n")


def build_client(url: str, token: str | None, timeout: float,
                 url_flag: str, token_flag: str, token_env: str) -> JazzXClient:
    return JazzXClient(
        gateway_url=normalize_gateway_url(require_value(url, name=url_flag)),
        token=require_value(token, name=token_flag, env_name=token_env),
        timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--gateway-url", default=os.getenv("JAZZX_GATEWAY_URL", ""))
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--label", default="A", help="Name for the first instance in reports")
    parser.add_argument("--gateway-url-b", default=os.getenv("JAZZX_GATEWAY_URL_B", ""),
                        help="Second instance. Supplying it turns on the comparison report")
    parser.add_argument("--token-b", default=os.getenv("JAZZX_TOKEN_B"),
                        help="Token for the second instance (they are rarely the same)")
    parser.add_argument("--label-b", default="B", help="Name for the second instance")
    parser.add_argument("--config-name", default="APP_CONFIG")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", type=Path, help="Single-instance mode: write the report here")
    parser.add_argument("--output-dir", type=Path,
                        help="Two-instance mode: write both audits and the comparison here")
    args = parser.parse_args()

    label_a, label_b = args.label, args.label_b
    if label_a == label_b:
        parser.error("--label and --label-b must differ; they name the columns in the report")

    print("Auditing ...", file=sys.stderr)
    client_a = build_client(args.gateway_url, args.token, args.timeout,
                            "--gateway-url", "--token", "JAZZX_TOKEN")
    report_a = audit_instance(client_a, args.config_name)

    # ---- single instance ----
    if not args.gateway_url_b:
        print_audit(report_a, label_a)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report_a, indent=2) + "\n", encoding="utf-8")
            print(f"\nReport written to {args.output}", file=sys.stderr)
        return 1 if report_a["unresolved_count"] else 0

    # ---- two instances ----
    client_b = build_client(args.gateway_url_b, args.token_b, args.timeout,
                            "--gateway-url-b", "--token-b", "JAZZX_TOKEN_B")
    if client_b.gateway_url == client_a.gateway_url:
        parser.error("--gateway-url and --gateway-url-b are the same instance")
    report_b = audit_instance(client_b, args.config_name)

    print_audit(report_a, label_a)
    print_audit(report_b, label_b)
    comparison = compare_reports(report_a, report_b, label_a, label_b)
    print_comparison(comparison)

    out_dir = args.output_dir
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for name, payload in ((f"{label_a}-audit.json", report_a),
                              (f"{label_b}-audit.json", report_b),
                              ("comparison.json", comparison)):
            path = out_dir / name
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            written.append(str(path))
        print("Reports written:\n  " + "\n  ".join(written), file=sys.stderr)

    # Unresolved ids on either side, or any divergence between them, is a failure.
    if comparison["problem_count"] or report_a["unresolved_count"] or report_b["unresolved_count"]:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApiError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
