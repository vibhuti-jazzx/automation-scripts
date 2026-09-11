#!/usr/bin/env python3
"""Add an existing JazzX project to a mortgage mock-server pipeline response."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from jazzx_api import ApiError, JazzXClient, parse_json_value, require_value, validate_uuid


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Add an existing Assistant project to the mortgage loan pipeline mock. "
        "No data is changed unless --apply is supplied."
    )
    parser.add_argument("--input", type=Path, required=True, help="Loan JSON file")
    parser.add_argument("--project-id", required=True, help="Existing Assistant project UUID")
    parser.add_argument(
        "--pipeline-loan-number",
        help="Loan number/name shown in the pipeline; defaults to the value in the JSON",
    )
    parser.add_argument("--gateway-url", "--base-url", default=os.getenv("JAZZX_GATEWAY_URL"))
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--pipeline-path", default="/mock-server/pipeline")
    parser.add_argument("--mock-data-path", default="/mock-server/api/mock-data")
    parser.add_argument("--priority", type=int, default=5)
    parser.add_argument("--initialize-if-missing", action="store_true")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--apply", action="store_true")
    return parser


def display_datetime(value: Any, fallback: str) -> str:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return fallback
    text = parsed.strftime("%m/%d/%Y %I:%M:%S %p")
    return text[1:] if text.startswith("0") else text


def borrower_name_from_raw(borrower: dict[str, Any]) -> str:
    first = str(borrower.get("firstName") or borrower.get("first_name") or "").strip()
    last = str(borrower.get("lastName") or borrower.get("last_name") or "").strip()
    return ", ".join(value for value in (last, first) if value) or "Unknown Borrower"


def loan_values(data: dict[str, Any]) -> dict[str, str]:
    details = parse_json_value(data.get("loan_details"))
    summary = parse_json_value(details.get("summary"))
    overview = parse_json_value(details.get("loanOverview"))
    loan = parse_json_value(data.get("loan"))
    core = parse_json_value(data.get("loan_core"))
    now = display_datetime(datetime.now().isoformat(), "")

    if loan:
        borrowers = data.get("borrowers") if isinstance(data.get("borrowers"), list) else []
        borrower = borrowers[0] if borrowers and isinstance(borrowers[0], dict) else {}
        product = parse_json_value(loan.get("loanProduct"))
        opened = display_datetime(loan.get("createdAt"), now)
        return {
            "number": str(loan.get("loanNumber") or "").strip(),
            "borrower": borrower_name_from_raw(borrower),
            "amount": str(loan.get("loanAmount") or 0),
            "type": f"{product.get('mortgageType') or 'Conventional'} {loan.get('loanPurpose') or 'Purchase'}",
            "stage": str(loan.get("currentLoanStage") or "Processing"),
            "opened": opened,
            "completion": display_datetime(loan.get("closingDate"), opened),
        }

    opened = display_datetime(core.get("application_date") or overview.get("applicationDate"), now)
    return {
        "number": str(core.get("loan_number") or summary.get("loanId") or "").strip(),
        "borrower": str(summary.get("borrowerName") or "Unknown Borrower"),
        "amount": str(core.get("base_loan_amount_usd") or summary.get("loanAmount") or 0),
        "type": f"{core.get('loan_type') or summary.get('loanType') or 'Conventional'} Purchase",
        "stage": str(core.get("loan_status") or summary.get("status") or "Processing"),
        "opened": opened,
        "completion": display_datetime(summary.get("closingDate"), opened),
    }


def empty_pipeline() -> dict[str, Any]:
    return {
        "outputVariables": {
            "loan_pipeline_data": {
                "items": [],
                "total": 0,
                "header": {"total_active_loans": 0},
                "filters": [],
            }
        }
    }


def pipeline_items(pipeline: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    output = pipeline.setdefault("outputVariables", {})
    if not isinstance(output, dict):
        raise ValueError("pipeline outputVariables must be an object")
    loan_data = output.setdefault("loan_pipeline_data", {})
    if not isinstance(loan_data, dict):
        raise ValueError("pipeline loan_pipeline_data must be an object")
    items = loan_data.setdefault("items", [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("pipeline items must be a list of objects")
    return loan_data, items


def pipeline_item_number(item: dict[str, Any]) -> str:
    fields = item.get("fields")
    return str(fields.get("Loan.LoanNumber") or "") if isinstance(fields, dict) else ""


def run(args: argparse.Namespace) -> int:
    if not args.input.is_file():
        raise ValueError(f"input file not found: {args.input}")
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read valid JSON from {args.input}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("loan JSON root must be an object")

    project_id = validate_uuid(args.project_id, name="--project-id")
    gateway = require_value(args.gateway_url, name="--gateway-url", env_name="JAZZX_GATEWAY_URL")
    token = require_value(args.token, name="--token", env_name="JAZZX_TOKEN")
    client = JazzXClient(gateway, token, args.timeout)
    values = loan_values(data)
    if args.pipeline_loan_number:
        values["number"] = args.pipeline_loan_number.strip()
    if not values["number"]:
        raise ValueError("no loan number found; pass --pipeline-loan-number")

    try:
        pipeline = client.request_json("GET", args.pipeline_path)
    except ApiError as exc:
        if not args.initialize_if_missing or "HTTP 404" not in str(exc):
            raise
        pipeline = empty_pipeline()
    if not isinstance(pipeline, dict):
        raise ValueError("pipeline response must be a JSON object")
    loan_data, items = pipeline_items(pipeline)

    duplicate = next(
        (
            item
            for item in items
            if str(item.get("loanId")) == project_id
            or pipeline_item_number(item) == values["number"]
        ),
        None,
    )
    if duplicate:
        print(f"Already present in pipeline: {values['number']} ({project_id})")
        return 0

    item = {
        "loanId": project_id,
        "fields": {
            "Loan.BorrowerName": values["borrower"],
            "Loan.LoanNumber": values["number"],
            "Loan.CurrentMilestoneName": values["stage"],
            "Loan.LoanAmount": values["amount"],
            "Loan.LastModified": display_datetime(datetime.now().isoformat(), ""),
            "Fields.1172": values["type"],
            "Loan.DateFileOpened": values["opened"],
            "Loan.DateOfEstimatedCompletion": values["completion"],
        },
    }
    print(json.dumps(item, indent=2))
    if not args.apply:
        print("Dry run only. Add --apply to create the updated pipeline mock.")
        return 0

    items.append(item)
    loan_data["total"] = len(items)
    header = loan_data.setdefault("header", {})
    if isinstance(header, dict):
        header["total_active_loans"] = len(items)
    filters = loan_data.get("filters")
    if isinstance(filters, list):
        for filter_item in filters:
            if isinstance(filter_item, dict) and filter_item.get("column") == "Loan.BorrowerName":
                values_list = filter_item.setdefault("values", [])
                if isinstance(values_list, list) and values["borrower"] not in values_list:
                    values_list.append(values["borrower"])

    payload = {
        "description": "Mortgage pipeline response updated by sync_loan_to_pipeline.py",
        "endpoint_path": "/pipeline",
        "method": "GET",
        "is_active": 1,
        "priority": args.priority,
        "response_status": 200,
        "response_body": pipeline,
    }
    client.request("POST", args.mock_data_path, json_body=payload, expected=(200, 201, 202))
    print(f"Added to pipeline: {values['number']} ({project_id})")
    return 0


def main() -> None:
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (ApiError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
