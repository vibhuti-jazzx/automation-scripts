#!/usr/bin/env python3
"""Delete a Knowledge Hub collection and archive its Assistant project."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from jazzx_api import ApiError, JazzXClient, require_value, validate_uuid


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delete a Knowledge Hub collection and archive the related Assistant project. "
        "The command is a dry run unless --execute is supplied."
    )
    parser.add_argument("--collection-id", required=True, help="Knowledge Hub collection UUID")
    project = parser.add_mutually_exclusive_group(required=True)
    project.add_argument("--project-id", help="Assistant project UUID")
    project.add_argument("--project-name", help="Exact Assistant project name")
    parser.add_argument("--gateway-url", default=os.getenv("JAZZX_GATEWAY_URL"))
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--execute", action="store_true", help="Perform the destructive operations")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation")
    return parser


def project_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("project_name") or "")


def find_project_by_name(client: JazzXClient, name: str) -> dict[str, Any]:
    matches = [
        item
        for item in client.iter_offset_pages("/assistant/api/v1/projects", page_size=100)
        if project_name(item) == name
    ]
    if not matches:
        raise ValueError(f"no project found with exact name {name!r}")
    if len(matches) > 1:
        ids = ", ".join(str(item.get("id", "<missing>")) for item in matches)
        raise ValueError(f"multiple projects have the name {name!r}: {ids}; use --project-id")
    if not matches[0].get("id"):
        raise ApiError("matched project response has no id")
    return matches[0]


def confirm(collection_id: str, project_id: str) -> None:
    if not sys.stdin.isatty():
        raise ValueError("interactive confirmation is unavailable; rerun with --yes")
    expected = f"delete {collection_id}"
    answer = input(
        f"This will delete collection {collection_id} and archive project {project_id}.\n"
        f"Type {expected!r} to continue: "
    ).strip()
    if answer != expected:
        raise KeyboardInterrupt("confirmation did not match")


def run(args: argparse.Namespace) -> int:
    gateway_url = require_value(args.gateway_url, name="--gateway-url", env_name="JAZZX_GATEWAY_URL")
    token = require_value(args.token, name="--token", env_name="JAZZX_TOKEN")
    collection_id = validate_uuid(args.collection_id, name="--collection-id")
    client = JazzXClient(gateway_url, token, args.timeout)

    if args.project_name:
        project = find_project_by_name(client, args.project_name)
        project_id = validate_uuid(str(project["id"]), name="resolved project id")
        resolved_name = project_name(project)
    else:
        project_id = validate_uuid(args.project_id, name="--project-id")
        resolved_name = "<not resolved>"

    print(f"Collection: {collection_id}")
    print(f"Project:    {project_id} ({resolved_name})")
    if not args.execute:
        print("Dry run only. Add --execute to delete the collection and archive the project.")
        return 0
    if not args.yes:
        confirm(collection_id, project_id)

    failures: list[str] = []
    try:
        client.request(
            "DELETE",
            f"/knowledge_hub/api/v1/collections/{collection_id}",
            expected=(200, 202, 204, 404),
        )
        print(f"Deleted collection {collection_id} (or it was already absent).")
    except ApiError as exc:
        failures.append(f"collection deletion failed: {exc}")

    try:
        client.request(
            "PATCH",
            f"/assistant/api/v1/project/{project_id}",
            json_body={"status": "archived"},
            expected=(200, 202, 204),
        )
        print(f"Archived project {project_id}.")
    except ApiError as exc:
        failures.append(f"project archival failed: {exc}")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    return 0


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
