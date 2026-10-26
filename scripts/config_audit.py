#!/usr/bin/env python3
"""Audit every UUID referenced by a JazzX instance's global configuration.

The deployment checker validates whether an instance is healthy. This script has
one narrower job: read APP_CONFIG, find every UUID anywhere in its value, and
resolve each UUID against the same instance as an ontology, collection, entity,
or nothing.

Every request is read-only. The report is written as both JSON and Markdown.
When a second instance is supplied, the reports are compared by config key,
resolved kind, name, and detail; UUID values themselves are expected to differ.

    export JAZZX_TOKEN="<bearer token>"
    python3 scripts/config_audit.py \
        --gateway-url https://poc-gw.jazzx.co \
        --label poc \
        --output-dir config-audit/

Exit codes: 0 when all references resolve, 1 when a reference is unresolved or
the cross-instance comparison finds drift, and 2 when the audit cannot run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
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

GLOBAL_CONFIG_PATH = "/knowledge_hub/api/v1/global-config/"
ENTITIES_PATH = "/knowledge_hub/api/v1/reasoning/entities"
ONTOLOGIES_PATH = "/knowledge_hub/api/v1/reasoning/ontologies"
COLLECTIONS_PATH = "/knowledge_hub/api/v1/collections"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
PAGE_LIMIT = 500


def find_uuids(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Return every UUID-looking string and its path inside a nested value."""
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            found.extend(find_uuids(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(find_uuids(item, f"{path}[{index}]"))
    elif isinstance(value, str) and UUID_RE.match(value.strip()):
        found.append((path, value.strip()))
    return found


def fetch_all(client: JazzXClient, path: str, **params: Any) -> list[dict[str, Any]]:
    """Page a list endpoint until it returns a short page."""
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


def resource_exists(client: JazzXClient, path: str) -> dict[str, Any] | None:
    """Read one resource; return None when it does not exist or cannot be read."""
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
    """Resolve every UUID in one instance's named global configuration."""
    configs = extract_items(
        client.request("GET", GLOBAL_CONFIG_PATH, params={"name": config_name}).json()
    )
    match = next(
        (c for c in configs if str(c.get("name") or c.get("key") or "") == config_name),
        None,
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
        detail = resource_exists(
            client, f"{GLOBAL_CONFIG_PATH.rstrip('/')}/{config_id}"
        ) or {}
        raw_value = detail.get("value", detail.get("config", detail))
    value = parse_json_value(raw_value) or (
        raw_value if isinstance(raw_value, dict) else {}
    )
    references = find_uuids(value)
    print(
        f"  {client.gateway_url}: config {config_id}, {len(references)} UUID references",
        file=sys.stderr,
    )

    ontologies = {
        str(row.get("id")): row
        for row in fetch_all(client, ONTOLOGIES_PATH)
        if row.get("id")
    }
    rows: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for key_path, candidate in references:
        if candidate not in seen:
            kind, name, detail = "UNRESOLVED", "", "not an ontology, collection or entity"
            if candidate in ontologies:
                kind = "ontology"
                name = str(ontologies[candidate].get("name") or "")
                detail = ""
            else:
                collection = resource_exists(client, f"{COLLECTIONS_PATH}/{candidate}")
                if collection is not None:
                    kind = "collection"
                    name = str(collection.get("name") or "")
                    detail = ""
                else:
                    entity = resource_exists(client, f"{ENTITIES_PATH}/{candidate}")
                    if entity is not None:
                        kind = "entity"
                        name = str(entity.get("name") or "")
                        detail = str(entity.get("entity_type") or "")
            seen[candidate] = {
                "id": candidate,
                "resolved_as": kind,
                "name": name,
                "detail": detail,
            }
        rows.append({**seen[candidate], "config_key": key_path})

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["resolved_as"]] = counts.get(row["resolved_as"], 0) + 1
    return {
        "gateway_url": client.gateway_url,
        "config_name": config_name,
        "config_id": config_id,
        "reference_count": len(rows),
        "counts_by_kind": counts,
        "unresolved_count": sum(row["resolved_as"] == "UNRESOLVED" for row in rows),
        "references": rows,
    }


COMPARED_FIELDS = ("resolved_as", "name", "detail")


def compare_reports(
    a: dict[str, Any], b: dict[str, Any], label_a: str, label_b: str
) -> dict[str, Any]:
    """Compare two audits while ignoring per-instance UUID differences."""
    by_key_a = {row["config_key"]: row for row in a["references"]}
    by_key_b = {row["config_key"]: row for row in b["references"]}
    rows: list[dict[str, Any]] = []
    for key in sorted(set(by_key_a) | set(by_key_b)):
        left, right = by_key_a.get(key), by_key_b.get(key)
        if left is None or right is None:
            rows.append(
                {
                    "config_key": key,
                    "verdict": f"MISSING_IN_{label_b.upper()}"
                    if right is None
                    else f"MISSING_IN_{label_a.upper()}",
                    "differs_on": ["config_key"],
                    label_a: left,
                    label_b: right,
                }
            )
            continue
        differs = [
            field
            for field in COMPARED_FIELDS
            if (left.get(field) or "") != (right.get(field) or "")
        ]
        rows.append(
            {
                "config_key": key,
                "verdict": "MATCH" if not differs else "MISMATCH",
                "differs_on": differs,
                "same_id": left["id"] == right["id"],
                label_a: left,
                label_b: right,
            }
        )
    problems = [row for row in rows if row["verdict"] != "MATCH"]
    return {
        "label_a": label_a,
        "label_b": label_b,
        "gateway_a": a["gateway_url"],
        "gateway_b": b["gateway_url"],
        "config_name": a["config_name"],
        "compared_fields": list(COMPARED_FIELDS),
        "key_count": len(rows),
        "match_count": len(rows) - len(problems),
        "problem_count": len(problems),
        "shared_id_count": sum(1 for row in rows if row.get("same_id")),
        "rows": rows,
    }


def _cell(value: Any) -> str:
    text = "-" if value in (None, "") else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_markdown(report: dict[str, Any], label: str) -> str:
    lines = [
        f"# Configuration UUID audit — {label}",
        "",
        f"- Gateway: `{report['gateway_url']}`",
        f"- Configuration: `{report['config_name']}`",
        f"- Configuration ID: `{report['config_id']}`",
        f"- UUID references: {report['reference_count']}",
        f"- Unresolved: {report['unresolved_count']}",
        "",
        "| Config key | UUID | Resolves to | Name | Detail | Valid |",
        "|---|---|---|---|---|---|",
    ]
    for row in report["references"]:
        valid = "no" if row["resolved_as"] == "UNRESOLVED" else "yes"
        lines.append(
            f"| {_cell(row['config_key'])} | `{_cell(row['id'])}` | "
            f"{_cell(row['resolved_as'])} | {_cell(row['name'])} | "
            f"{_cell(row['detail'])} | {valid} |"
        )
    lines.append("")
    counts = ", ".join(
        f"{key}: {value}" for key, value in sorted(report["counts_by_kind"].items())
    ) or "none"
    lines.append(f"Summary: {counts}.")
    return "\n".join(lines) + "\n"


def render_comparison_markdown(comparison: dict[str, Any]) -> str:
    a, b = comparison["label_a"], comparison["label_b"]
    lines = [
        f"# Configuration comparison — {a} vs {b}",
        "",
        "UUID values are expected to differ between instances. The comparison checks "
        "what each UUID resolves to.",
        "",
        f"- Matches: {comparison['match_count']}",
        f"- Differences: {comparison['problem_count']}",
        "",
        f"| Config key | Verdict | {a} | {b} |",
        "|---|---|---|---|",
    ]
    for row in comparison["rows"]:
        left = row.get(a)
        right = row.get(b)
        describe = lambda item: _cell(
            f"{item.get('resolved_as')}: {item.get('name') or item.get('detail') or '-'}"
        ) if item else "absent"
        lines.append(
            f"| {_cell(row['config_key'])} | {row['verdict']} | "
            f"{describe(left)} | {describe(right)} |"
        )
    return "\n".join(lines) + "\n"


def build_client(url: str, token: str | None, timeout: float, url_flag: str, token_flag: str,
                 token_env: str) -> JazzXClient:
    return JazzXClient(
        gateway_url=normalize_gateway_url(require_value(url, name=url_flag)),
        token=require_value(token, name=token_flag, env_name=token_env),
        timeout=timeout,
    )


def write_audit_report(report: dict[str, Any], label: str, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{label}-audit.json"
    markdown_path = output_dir / f"{label}-audit.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report, label), encoding="utf-8")
    return [json_path, markdown_path]


def print_audit(report: dict[str, Any], label: str) -> None:
    print("\n" + "=" * 96)
    print(f"CONFIGURATION UUID AUDIT — {label} — {report['gateway_url']}")
    print("=" * 96)
    for row in report["references"]:
        shown = row["name"] or row["detail"] or "-"
        print(f"  {row['resolved_as']:<12} {row['config_key']:<44} {shown}")
    print(
        f"\n  {report['reference_count']} reference(s), "
        f"{report['unresolved_count']} unresolved."
    )
    print("=" * 96 + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--gateway-url", default=os.getenv("JAZZX_GATEWAY_URL", ""))
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--label", default="instance")
    parser.add_argument("--gateway-url-b", default=os.getenv("JAZZX_GATEWAY_URL_B", ""))
    parser.add_argument("--token-b", default=os.getenv("JAZZX_TOKEN_B"))
    parser.add_argument("--label-b", default="reference")
    parser.add_argument("--config-name", default="APP_CONFIG")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("config-audit"),
        help="Directory for JSON and Markdown reports (default: config-audit/)",
    )
    args = parser.parse_args(argv)
    if args.label == args.label_b and args.gateway_url_b:
        parser.error("--label and --label-b must differ")

    print("Auditing configuration UUIDs ...", file=sys.stderr)
    client_a = build_client(
        args.gateway_url, args.token, args.timeout, "--gateway-url", "--token", "JAZZX_TOKEN"
    )
    report_a = audit_instance(client_a, args.config_name)
    print_audit(report_a, args.label)
    written = write_audit_report(report_a, args.label, args.output_dir)

    if not args.gateway_url_b:
        print("Reports written:\n  " + "\n  ".join(map(str, written)), file=sys.stderr)
        return 1 if report_a["unresolved_count"] else 0

    client_b = build_client(
        args.gateway_url_b, args.token_b, args.timeout,
        "--gateway-url-b", "--token-b", "JAZZX_TOKEN_B",
    )
    if client_b.gateway_url == client_a.gateway_url:
        parser.error("--gateway-url and --gateway-url-b are the same instance")
    report_b = audit_instance(client_b, args.config_name)
    print_audit(report_b, args.label_b)
    comparison = compare_reports(report_a, report_b, args.label, args.label_b)
    comparison_json = args.output_dir / "comparison.json"
    comparison_md = args.output_dir / "comparison.md"
    comparison_json.write_text(json.dumps(comparison, indent=2) + "\n", encoding="utf-8")
    comparison_md.write_text(render_comparison_markdown(comparison), encoding="utf-8")
    written.extend(write_audit_report(report_b, args.label_b, args.output_dir))
    written.extend([comparison_json, comparison_md])
    print("Reports written:\n  " + "\n  ".join(map(str, written)), file=sys.stderr)
    return 1 if (
        report_a["unresolved_count"]
        or report_b["unresolved_count"]
        or comparison["problem_count"]
    ) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApiError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
