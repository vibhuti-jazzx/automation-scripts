#!/usr/bin/env python3
"""Compare a source loan collection against its clone and report the differences.

Read-only: every call is a GET. Nothing is created, patched or deleted.

This answers "was the loan cloned properly?" independently of the clone run --
it reads both collections back from the API rather than trusting whatever the
clone process reported about itself.

Usage:
    export JAZZX_GATEWAY_URL="https://<your-gateway-host>"
    export JAZZX_TOKEN="<bearer token>"
    python verify_clone_pair.py \
        --source-collection-id <source-collection-uuid> \
        --target-collection-id <target-collection-uuid> \
        --remove-macer
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Reuse the clone script's client so this inherits its retry/timeout handling
# rather than reimplementing a second, subtly different HTTP layer.
_spec = importlib.util.spec_from_file_location(
    "_loan_clone", Path(__file__).resolve().parent / "loan_clone.py"
)
_lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lc)


def counts_by_type(entities: list[dict[str, Any]]) -> Counter:
    return Counter(str(entity.get("entity_type") or "Unknown") for entity in entities)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-collection-id", required=True)
    parser.add_argument("--target-collection-id", required=True)
    parser.add_argument("--gateway-url", default=os.getenv("JAZZX_GATEWAY_URL", ""))
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--remove-macer", action="store_true",
                        help="The clone was run with --remove-macer, so MACER types "
                             "are expected to be absent from the target")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    client = _lc.JazzXClient(args.gateway_url, args.token, None,
                             "JazzX-Clone-Verify/1.0", args.timeout, args.timeout)

    print("Reading source collection ...", file=sys.stderr)
    source_entities = client.entities_in_collection(args.source_collection_id)
    source_documents = client.documents_in_collection(args.source_collection_id)
    print("Reading target collection ...", file=sys.stderr)
    target_entities = client.entities_in_collection(args.target_collection_id)
    target_documents = client.documents_in_collection(args.target_collection_id)

    source_counts = counts_by_type(source_entities)
    target_counts = counts_by_type(target_entities)
    macer = _lc.MACER_ENTITY_TYPES

    rows, problems = [], []
    for entity_type in sorted(set(source_counts) | set(target_counts)):
        in_source = source_counts.get(entity_type, 0)
        in_target = target_counts.get(entity_type, 0)
        is_macer = entity_type in macer
        expected = 0 if (is_macer and args.remove_macer) else in_source
        ok = in_target == expected
        rows.append({"entity_type": entity_type, "in_source": in_source,
                     "in_target": in_target, "expected_in_target": expected,
                     "macer": is_macer, "ok": ok})
        if not ok:
            problems.append(
                f"{entity_type}: target has {in_target}, expected {expected} "
                f"(source has {in_source})"
            )

    doc_ok = len(target_documents) == len(source_documents)
    if not doc_ok:
        problems.append(
            f"documents: target has {len(target_documents)}, source has {len(source_documents)}"
        )

    # A clone that still points at the source collection's ids is not isolated.
    source_ids = {str(entity.get("id")) for entity in source_entities}
    leaked = []
    for entity in target_entities:
        hits = {value for value in _lc.nested_values(entity.get("json_value") or {})
                if isinstance(value, str) and value in source_ids}
        if hits:
            leaked.append({"target_entity": str(entity.get("id")), "source_ids": sorted(hits)})
    if leaked:
        problems.append(f"{len(leaked)} target entities still reference source entity ids")

    placeholders = [str(entity.get("id")) for entity in target_entities
                    if any(isinstance(v, str) and v.startswith(_lc.PLACEHOLDER_PREFIXES)
                           for v in _lc.nested_values(entity.get("json_value") or {}))]
    if placeholders:
        problems.append(f"{len(placeholders)} target entities still hold unresolved placeholders")

    width = max((len(row["entity_type"]) for row in rows), default=20) + 2
    print("\n" + "=" * (width + 42))
    print("CLONE VERIFICATION  (read back from the API)")
    print("=" * (width + 42))
    print(f"\nsource {args.source_collection_id}\ntarget {args.target_collection_id}\n")
    print(f"  {'entity type':<{width}}{'source':>8}{'target':>8}{'expect':>8}   ok")
    print("  " + "-" * (width + 32))
    for row in rows:
        tag = " (macer)" if row["macer"] else ""
        print(f"  {row['entity_type'] + tag:<{width}}{row['in_source']:>8}"
              f"{row['in_target']:>8}{row['expected_in_target']:>8}"
              f"   {'yes' if row['ok'] else 'NO'}")
    print(f"\n  {'documents':<{width}}{len(source_documents):>8}{len(target_documents):>8}"
          f"{len(source_documents):>8}   {'yes' if doc_ok else 'NO'}")
    print(f"\n  entities total{'':<{max(0, width - 14)}}"
          f"{len(source_entities):>8}{len(target_entities):>8}")

    if leaked:
        print(f"\n  LEAKED SOURCE IDS in {len(leaked)} target entities, first few:")
        for item in leaked[:5]:
            print(f"    {item['target_entity']}: {item['source_ids'][:3]}")
    if placeholders:
        print(f"\n  UNRESOLVED PLACEHOLDERS in {len(placeholders)} entities: {placeholders[:5]}")

    print("\n" + ("VERDICT: clone looks correct" if not problems else "VERDICT: PROBLEMS FOUND"))
    for problem in problems:
        print(f"  - {problem}")
    print("=" * (width + 42) + "\n")

    report = {"source_collection_id": args.source_collection_id,
              "target_collection_id": args.target_collection_id,
              "remove_macer": args.remove_macer,
              "source_entity_total": len(source_entities),
              "target_entity_total": len(target_entities),
              "source_document_count": len(source_documents),
              "target_document_count": len(target_documents),
              "by_entity_type": rows,
              "leaked_source_ids": leaked,
              "unresolved_placeholder_entities": placeholders,
              "problems": problems,
              "ok": not problems}
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    return 0 if not problems else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
