#!/usr/bin/env python3
"""Delete every document and entity from a collection, so a clone can be redone.

DESTRUCTIVE AND IRREVERSIBLE. Dry run is the default: nothing is deleted unless
--confirm is passed, and --confirm requires the collection id to be repeated so
a copy-pasted command cannot wipe the wrong collection.

The collection and its project survive -- only their contents are removed -- so
a clone can be re-run into the same --target-collection-id afterwards.

    export JAZZX_GATEWAY_URL="https://<your-gateway-host>"
    export JAZZX_TOKEN="<bearer token>"

    # see what would go
    python reset_target_collection.py --collection-id <collection-uuid>

    # actually delete
    python reset_target_collection.py --collection-id <collection-uuid> \
        --confirm <collection-uuid>
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from collections import Counter
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_loan_clone", Path(__file__).resolve().parent / "loan_clone.py"
)
_lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--collection-id", required=True)
    parser.add_argument("--confirm", metavar="COLLECTION_ID",
                        help="Repeat the collection id to actually delete. Without this "
                             "the script only reports what it would remove")
    parser.add_argument("--gateway-url", default=os.getenv("JAZZX_GATEWAY_URL", ""))
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--documents", dest="documents", action="store_true", default=True)
    parser.add_argument("--no-documents", dest="documents", action="store_false")
    parser.add_argument("--entities", dest="entities", action="store_true", default=True)
    parser.add_argument("--no-entities", dest="entities", action="store_false")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()

    live = args.confirm is not None
    if live and args.confirm != args.collection_id:
        parser.error(
            f"--confirm must repeat --collection-id exactly.\n"
            f"  --collection-id {args.collection_id}\n"
            f"  --confirm       {args.confirm}"
        )

    client = _lc.JazzXClient(args.gateway_url, args.token, None,
                             "JazzX-Collection-Reset/1.0", args.timeout, args.timeout)

    documents = client.documents_in_collection(args.collection_id) if args.documents else []
    entities = client.entities_in_collection(args.collection_id) if args.entities else []

    print(f"\ncollection {args.collection_id}")
    print(f"  documents : {len(documents)}")
    print(f"  entities  : {len(entities)}")
    if entities:
        print("\n  entities by type:")
        for entity_type, count in Counter(
            str(e.get("entity_type") or "Unknown") for e in entities
        ).most_common():
            print(f"    {entity_type:<34}{count:>6}")

    if not live:
        print(f"\nDRY RUN -- nothing deleted. To delete all of the above, re-run with:")
        print(f"  --confirm {args.collection_id}\n")
        return 0

    print(f"\nDeleting {len(documents)} documents and {len(entities)} entities ...\n")
    doc_ok = doc_fail = ent_ok = ent_fail = 0

    for index, document in enumerate(documents, start=1):
        document_id = str(document.get("id") or document.get("document_id") or "")
        if not document_id:
            doc_fail += 1
            continue
        try:
            client.request("DELETE", f"/kernel/api/v1/collections/documents/{document_id}")
            doc_ok += 1
        except RuntimeError as exc:
            doc_fail += 1
            print(f"  document {document_id} failed: {exc}", file=sys.stderr)
        if index % 25 == 0:
            print(f"  documents {index}/{len(documents)}", file=sys.stderr)

    for index, entity in enumerate(entities, start=1):
        entity_id = str(entity.get("id") or "")
        if not entity_id:
            ent_fail += 1
            continue
        try:
            client.request("DELETE", f"/knowledge_hub/api/v1/reasoning/entities/{entity_id}")
            ent_ok += 1
        except RuntimeError as exc:
            ent_fail += 1
            print(f"  entity {entity_id} failed: {exc}", file=sys.stderr)
        if index % 50 == 0:
            print(f"  entities {index}/{len(entities)}", file=sys.stderr)

    print(f"\ndocuments deleted {doc_ok}, failed {doc_fail}")
    print(f"entities  deleted {ent_ok}, failed {ent_fail}")

    remaining_docs = client.documents_in_collection(args.collection_id)
    remaining_entities = client.entities_in_collection(args.collection_id)
    print(f"\nremaining: {len(remaining_docs)} documents, {len(remaining_entities)} entities")
    if remaining_docs or remaining_entities:
        print("Collection is NOT empty -- re-run to clear the remainder.")
        return 1
    print("Collection is empty. Re-run the clone into this same --target-collection-id.\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
