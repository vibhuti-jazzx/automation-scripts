#!/usr/bin/env python3
"""Reconstruct a reusable raw loan JSON document from Knowledge Hub entities.

The reconstructed document follows the same shape as 1441010.json, is
validated with validate_input.py, and is uploaded as a JSON document to a
target collection. With ``--create-project``, it first creates a visible
Assistant dashboard project and uses that project's collection. It does not
create mock-server data.

Example:
  python3 reconstruct_loan_json.py --loan-number 1441010 \
    --source-collection-id <SOURCE_COLLECTION_ID> \
    --create-project
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_GATEWAY_URL = os.getenv("JAZZX_GATEWAY_URL", "")


def validated_uuid(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID, got {value!r}") from exc


def load_validator() -> Any:
    path = Path(__file__).resolve().parent / "validate_input.py"
    spec = importlib.util.spec_from_file_location("loan_input_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load validator: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class JazzXClient:
    def __init__(self, token: str, user_session_id: str | None = None,
                 gateway_url: str = DEFAULT_GATEWAY_URL, timeout: float = 60):
        gateway_url = gateway_url.strip().rstrip("/")
        if not gateway_url.startswith(("https://", "http://")):
            raise ValueError("Pass --gateway-url or set JAZZX_GATEWAY_URL to an http(s) URL")
        token = token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise ValueError("JazzX token cannot be empty")
        if timeout <= 0:
            raise ValueError("--timeout must be greater than zero")
        self.gateway_url = gateway_url
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {token}", "Accept": "application/json",
            "User-Agent": "JazzX-Loan-Reconstructor/1.0",
        }
        self.project_headers = {**self.headers, "Content-Type": "application/json"}
        if user_session_id:
            self.project_headers["x-user-session-id"] = user_session_id

    def get(self, path: str, query: dict[str, Any] | None = None, *, project_api: bool = False) -> Any:
        url = f"{self.gateway_url}{path}"
        if query:
            url += "?" + urlencode(query)
        headers = self.project_headers if project_api else self.headers
        return self._request(Request(url, headers=headers, method="GET"), "GET")

    def upload_json(self, collection_id: str, filename: str, data: dict[str, Any]) -> Any:
        boundary = f"----JazzXLoanJson{uuid.uuid4().hex}"
        fields = {
            "filename": filename,
            "meta_data": json.dumps({"source": "knowledge-hub-reconstruction", "content_type": "application/json"}),
            "storage_only": "true",
            "build_knowledge_graph": "false",
        }
        body = bytearray()
        for name, value in fields.items():
            body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        content = json.dumps(data, indent=2).encode("utf-8")
        body.extend(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: text/plain\r\n\r\n".encode()
        )
        body.extend(content)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        request = Request(
            f"{self.gateway_url}/knowledge_hub/api/v1/collections/{collection_id}/newdocuments",
            data=bytes(body), headers={**self.headers, "Content-Type": f"multipart/form-data; boundary={boundary}", "X-Upload-Source": "internal"},
            method="POST",
        )
        return self._request(request, "POST")

    def create_project(self, name: str, description: str) -> dict[str, Any]:
        """Create an Assistant dashboard project and return its collection."""
        matches = self.active_projects_named(name)
        if matches:
            ids = ", ".join(str(item.get("id")) for item in matches)
            raise RuntimeError(f"Active project {name!r} already exists ({ids}); refusing a duplicate")
        payload = {
            "name": name,
            "description": description,
            "image_url": "/images/project/folderImages/teal-folder.svg",
            "display_color": "teal",
        }
        request = Request(
            f"{self.gateway_url}/assistant/api/v1/project/resources",
            data=json.dumps(payload).encode("utf-8"),
            headers=self.project_headers,
            method="POST",
        )
        project = self._request(request, "POST")
        if not isinstance(project, dict) or not project.get("id") or not project.get("collection_id"):
            raise RuntimeError(f"Project creation response is missing id or collection_id: {project}")
        return project

    def active_projects_named(self, name: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.get("/assistant/api/v1/projects", {"limit": 100, "offset": offset}, project_api=True)
            if isinstance(page, list):
                items = page
            elif isinstance(page, dict):
                items = page.get("items") or page.get("data") or []
            else:
                raise RuntimeError("project API returned an invalid list envelope")
            if not isinstance(items, list):
                raise RuntimeError("project API returned an invalid list envelope")
            matches.extend(
                item for item in items
                if isinstance(item, dict)
                and item.get("name") == name
                and str(item.get("status", "open")).lower() != "archived"
            )
            if len(items) < 100:
                return matches
            offset += len(items)

    def _request(self, request: Request, label: str) -> Any:
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                try:
                    return json.loads(raw) if raw else {}
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{label} returned invalid JSON") from exc
        except HTTPError as exc:
            raise RuntimeError(f"{label} failed ({exc.code}): {exc.read().decode('utf-8', 'replace')}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"{label} failed: {getattr(exc, 'reason', exc)}") from exc

    def entities(self, collection_id: str) -> list[dict[str, Any]]:
        result, seen, skip = [], set(), 0
        seen_pages: set[tuple[str, ...]] = set()
        while True:
            page = self.get("/knowledge_hub/api/v1/reasoning/entities", {
                "odata_query": f"collection_id eq '{collection_id}'", "skip": skip, "limit": 1000,
                "orderby": "created_at", "order_direction": "desc",
            })
            if isinstance(page, list):
                items = page
            elif isinstance(page, dict):
                items = page.get("items") or page.get("data") or []
            else:
                raise RuntimeError("entity API returned an invalid list envelope")
            if not isinstance(items, list):
                raise RuntimeError("entity API returned an invalid list envelope")
            if not items:
                return result
            signature = tuple(str(item.get("id") or "") for item in items if isinstance(item, dict))
            if signature in seen_pages:
                raise RuntimeError("entity pagination repeated a page; refusing an infinite loop")
            seen_pages.add(signature)
            for entity in items:
                if not isinstance(entity, dict):
                    continue
                if entity.get("id") not in seen:
                    result.append(entity)
                    seen.add(entity.get("id"))
            if len(items) < 1000:
                return result
            skip += 1000

def jv(entity: dict[str, Any]) -> dict[str, Any]:
    value = entity.get("json_value") or {}
    if isinstance(value, dict):
        return value
    # Some Knowledge Hub responses serialize json_value as a JSON string.
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass
    return {}


def first(values: list[dict[str, Any]]) -> dict[str, Any]:
    return jv(values[0]) if values else {}


def grouped(entities: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for entity in entities:
        result.setdefault(str(entity.get("entity_type", "Unknown")), []).append(entity)
    return result


def value(data: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if data.get(name) is not None:
            return data[name]
    return default


def nested_value(data: Any, *names: str, default: Any = None) -> Any:
    """Return the first matching value from a nested KH entity payload.

    LoanDetails deliberately nests dashboard fields (for example,
    ``loanOverview.principalAndInterestPayment``), so a shallow lookup is not
    sufficient when rebuilding the original input document.
    """
    wanted = {name.replace("_", "").lower() for name in names}
    if isinstance(data, dict):
        for key, item in data.items():
            if key.replace("_", "").lower() in wanted and item is not None:
                return item
        for item in data.values():
            found = nested_value(item, *names, default=None)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = nested_value(item, *names, default=None)
            if found is not None:
                return found
    return default


def address(raw: dict[str, Any]) -> dict[str, Any]:
    if not raw:
        return {}
    return {
        "line": value(raw, "street_line_1", "line", "street", "full_street_address", "streetLine1"),
        "city": raw.get("city"),
        "stateCode": value(raw, "state", "state_code", "stateCode"),
        "zipCode": value(raw, "postal_code", "zip_code", "zipCode"),
    }


def reconstruct(loan_number: str, entities: list[dict[str, Any]]) -> dict[str, Any]:
    by_type = grouped(entities)
    loan_core = first(by_type.get("LoanCore", []))
    loan_metrics = first(by_type.get("LoanMetrics", []))
    subject = first(by_type.get("SubjectProperty", []))
    details = first(by_type.get("LoanDetails", []))

    lien = {"First": "FirstLien", "Second": "SecondLien", "Third": "ThirdLien"}.get(loan_core.get("lien_priority"), loan_core.get("lien_priority"))
    amortization = {"Fixed": "FixedRateMortgage"}.get(loan_core.get("amortization_type"), loan_core.get("amortization_type"))
    principal_payment = value(loan_metrics, "principal_and_interest_payment_usd", "principalAndInterestPayment")
    if principal_payment is None:
        principal_payment = value(loan_core, "principal_and_interest_payment_usd", "principalAndInterestPayment")
    if principal_payment is None:
        principal_payment = nested_value(details, "principalAndInterestPayment", "principal_and_interest_payment")
    loan = {
        "loanNumber": value(loan_core, "loan_number", "external_loan_number", default=loan_number),
        "loanType": value(loan_core, "loan_type", default="Conventional"),
        "loanPurpose": value(loan_core, "loan_purpose", default="Purchase"),
        "loanAmount": value(loan_core, "total_loan_amount_usd", "base_loan_amount_usd", default=0),
        "principalAndInterestPayment": principal_payment,
        "currentLoanStage": value(loan_core, "loan_status", default="Application"),
        "createdAt": (
            f"{loan_core['application_date']}T00:00:00Z"
            if isinstance(loan_core.get("application_date"), str) and "T" not in loan_core["application_date"]
            else loan_core.get("application_date")
        ),
        "closingDate": value(loan_core, "expected_closing_date", "actual_closing_date"),
        "loanOfficer": value(loan_metrics, "loan_officer", "loanOfficer") or nested_value(details, "loanOfficer", "loan_officer"),
        "borrowersWillPayTIFromEscrowIndicator": not bool(loan_core.get("is_escrow_waived", False)),
        "loanProduct": {
            "lienType": lien, "mortgageType": loan_core.get("loan_type"), "loanAmortizationType": amortization,
            "pricingEngineNoteRate": loan_core.get("note_rate_percent"), "apr": loan_core.get("apr_percent"),
            "loanTermMonthsCount": loan_core.get("term_months"),
        },
        "downPayment": {
            "amount": value(loan_core.get("down_payment") or {}, "total_amount_usd", default=0),
            "percentage": value(loan_core.get("down_payment") or {}, "percent_of_purchase_price", default=0),
        },
        "ltvPercent": loan_core.get("ltv_percent"), "cltvPercent": loan_core.get("cltv_percent"), "hcltvPercent": loan_core.get("hcltv_percent"),
        "concurrentFinancings": [], "closingCosts": [],
    }

    borrower_entities = by_type.get("Borrower", [])
    # Relation fields may use either the Knowledge Hub entity ID or the
    # external borrower ID stored in json_value.id.  Support both forms.
    borrower_index: dict[str, int] = {}
    for index, entity in enumerate(borrower_entities):
        for identifier in (entity.get("id"), jv(entity).get("id")):
            if identifier:
                borrower_index[str(identifier)] = index
    borrowers = []
    for entity in borrower_entities:
        data = jv(entity)
        declarations = {
            "ownershipInterestInPropertyLast3Years": data.get("ownership_interest_in_property_last_3_years", False),
            "outstandingJudgments": data.get("has_outstanding_judgments", False),
            "declaredBankruptcy": data.get("has_declared_bankruptcy", False),
            "propertyForeclosed": data.get("has_property_foreclosed", False),
            "partyToLawsuit": data.get("is_party_to_lawsuit", False),
            "conveyedTitleInLieu": data.get("has_conveyed_title_in_lieu", False),
            "preforeclosureShortSale": data.get("has_preforeclosure_short_sale", False),
            "delinquentOnFederalDebt": data.get("is_delinquent_on_federal_debt", False),
            "obligatedOnOtherLoan": data.get("is_obligated_on_other_loan", False),
            "alimonyChildSupport": data.get("has_alimony_child_support", False),
            "coSignerOnOtherDebt": data.get("is_co_signer_on_other_debt", False),
            "borrowedDownPayment": data.get("has_borrowed_down_payment", False),
        }
        borrowers.append({
            "firstName": data.get("first_name"), "middleName": data.get("middle_name"), "lastName": data.get("last_name"),
            "ssnMasked": data.get("ssn_masked"), "dateOfBirth": data.get("date_of_birth"), "emailAddress": data.get("email"),
            "cellPhoneNumber": data.get("phone_cell"), "citizenshipType": data.get("citizenship_status"),
            "maritalStatus": data.get("marital_status"), "dependentCount": data.get("dependent_count", 0),
            "dependentAges": data.get("dependent_ages", []), "currentAddress": address(data.get("current_address") or {}),
            "mailingAddressSameAsCurrent": data.get("mailing_address_same_as_current", True),
            "firstTimeHomebuyerIndicator": data.get("is_first_time_homebuyer", False),
            "intentToOccupy": data.get("will_occupy_as_primary_residence", True), "declarations": declarations,
            "hmdaEthnicity": data.get("hmda_ethnicity"), "hmdaRace": data.get("hmda_race", []), "hmdaSex": data.get("hmda_sex"),
            "assets": [], "incomes": [], "liabilities": [],
        })

    def borrower_for(data: dict[str, Any]) -> dict[str, Any] | None:
        identifier = value(data, "borrower_id", "borrowerId", "borrower_external_id", "borrowerExternalId")
        index = borrower_index.get(str(identifier)) if identifier else None
        return borrowers[index] if index is not None else None

    for entity in by_type.get("Asset", []):
        data = jv(entity); borrower = borrower_for(data)
        if borrower is not None:
            borrower["assets"].append({"assetType": data.get("asset_type"), "institutionName": data.get("institution_name"),
                "accountNumber": data.get("account_number_masked"), "currentBalance": data.get("current_balance_usd"),
                "isLiquid": data.get("is_liquid", True), "isGift": data.get("is_gift", False),
                "willBeUsedForClosing": data.get("will_be_used_for_closing", False), "institutionAddress": address(data.get("institution_address") or {})})
    for entity in by_type.get("IncomeSource", []) + by_type.get("Income", []):
        data = jv(entity); borrower = borrower_for(data)
        if borrower is not None:
            borrower["incomes"].append({"incomeType": data.get("income_type"), "totalCalculatedQualifiedMonthlyIncome": data.get("monthly_amount_usd"),
                "frequency": data.get("frequency"), "isPrimary": data.get("is_primary"), "documentationLevel": data.get("documentation_level"), "verificationStatus": data.get("verification_status")})
    for entity in by_type.get("Liability", []):
        data = jv(entity); borrower = borrower_for(data)
        if borrower is not None:
            borrower["liabilities"].append({"liabilityType": data.get("liability_type"), "creditorName": data.get("creditor_name"),
                "accountNumber": data.get("account_number_masked"), "unpaidBalance": data.get("unpaid_balance_usd"), "monthlyPaymentAmount": data.get("monthly_payment_usd"),
                "monthsRemaining": data.get("months_remaining"), "lienPosition": data.get("lien_position"), "isSubjectPropertyLien": data.get("is_subject_property_lien", False),
                "willBePaidOffAtClosing": data.get("will_be_paid_off_at_closing", False), "isExcludedFromDti": data.get("is_excluded_from_dti", False)})
    def detail_credit_scores(borrower: dict[str, Any]) -> list[dict[str, Any]]:
        """Fallback for credit data summarized in LoanDetails."""
        try:
            borrower_position = borrowers.index(borrower)
            profile = details.get("borrowerDetails", [])[borrower_position].get("borrowerProfile", {})
            credit = profile.get("creditScore") or {}
            breakdown = credit.get("bureauBreakdown") or {}
        except (IndexError, AttributeError, ValueError):
            return []
        bureau_names = {"equifax": "Equifax", "experian": "Experian", "transUnion": "TransUnion"}
        return [
            {"bureau": bureau, "score": score, "creditModelType": None, "creditScoreFactors": []}
            for key, bureau in bureau_names.items()
            if (score := breakdown.get(key)) is not None
        ]

    credit_reports = by_type.get("CreditReport", [])
    for entity in credit_reports:
        data = jv(entity); borrower = borrower_for(data)
        # This collection has one borrower and one CreditReport.  If POC2 has
        # omitted or rewritten the relationship field, that pairing is still
        # unambiguous and should not discard the report.
        if borrower is None and len(borrowers) == 1 and len(credit_reports) == 1:
            borrower = borrowers[0]
        if borrower is not None:
            scores = [
                {"bureau": score.get("bureau"), "score": score.get("score"),
                 "creditModelType": score.get("score_model"),
                 "creditScoreFactors": [{"code": code, "description": ""} for code in score.get("reason_codes", [])]}
                for score in data.get("scores", [])
            ]
            if not scores:
                scores = detail_credit_scores(borrower)
            valid_scores = sorted((score for score in scores if isinstance(score.get("score"), int)), key=lambda score: score["score"])
            median_score = valid_scores[len(valid_scores) // 2] if valid_scores else None
            borrower.update({"creditProvider": data.get("credit_provider"), "creditReportType": data.get("report_type"),
                "creditReportIssuedDate": data.get("report_date"), "creditReferenceNumber": data.get("report_reference_number"),
                "openTradelines": data.get("open_trade_lines"), "totalTradelines": data.get("total_trade_lines"),
                "creditScores": scores, "medianCreditScore": median_score})

    subject_property = {
        "propertyType": subject.get("property_type"), "intendedUsageType": subject.get("occupancy_type"),
        "numberOfUnits": subject.get("number_of_units"), "yearBuilt": subject.get("year_built"), "legalDescription": subject.get("legal_description"),
        "apn": subject.get("apn"), "lotSizeAcres": subject.get("lot_size_acres"), "lotSizeSqft": subject.get("lot_size_sqft"),
        "livingAreaSqft": subject.get("living_area_sqft"), "bedrooms": subject.get("bedrooms"), "bathrooms": subject.get("bathrooms"),
        "constructionType": subject.get("construction_type"), "isManufactured": subject.get("is_manufactured", False),
        "manufacturedWidthType": subject.get("manufactured_width_type"), "isPUD": subject.get("is_pud", False), "isCondo": subject.get("is_condo", False),
        "condoProjectClassification": subject.get("condo_project_classification"), "hoaMonthlyDues": subject.get("hoa_monthly_usd"),
        "purchasePrice": subject.get("purchase_price_usd"), "estimatedValueAmount": subject.get("estimated_value_usd"),
        "appraisedValue": subject.get("appraised_value_usd"), "appraisalDate": subject.get("appraisal_date"),
        "improvementsCost": subject.get("improvements_cost_usd"), "landValue": subject.get("land_value_usd"),
        "isMixedUse": subject.get("is_mixed_use", False), "mixedUseCommercialPercent": subject.get("mixed_use_commercial_percent"),
        "floodZone": subject.get("flood_zone"), "isInSpecialFloodHazardArea": subject.get("is_in_special_flood_hazard_area", False),
        "titleMannerHeld": subject.get("title_manner_held"), "address": address(subject.get("address") or {}),
    }
    description = value(details, "summary", default={})
    primary = borrowers[0] if borrowers else {}
    return {"project_name": loan_number, "project_description": f"{primary.get('firstName', '')} {primary.get('lastName', '')}".strip() or str(description),
            "loan": loan, "borrowers": borrowers, "subject_property": subject_property, "properties": []}


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconstruct, validate, and upload a raw loan JSON document.")
    parser.add_argument("--loan-number", required=True, help="Name used for the reconstructed JSON document")
    parser.add_argument("--source-collection-id", required=True, help="Collection containing the source loan entities")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--target-collection-id", help="Existing collection to receive the generated JSON document")
    destination.add_argument("--create-project", action="store_true", help="Create an Assistant dashboard project and upload into its new collection")
    parser.add_argument("--project-name", help="Name for the new dashboard project (defaults to the loan number)")
    parser.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL, help="JazzX gateway URL (or set JAZZX_GATEWAY_URL)")
    parser.add_argument("--user-session-id", default=os.getenv("JAZZX_USER_SESSION_ID"), help="Optional JazzX user-session ID required by some POC2 project APIs")
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--output", type=Path, help="Optional local output path")
    parser.add_argument("--inspect", action="store_true", help="List source entity types and IDs without reconstructing or uploading")
    parser.add_argument("--dry-run", action="store_true", help="Reconstruct and validate without creating a project or uploading")
    args = parser.parse_args()
    if not args.token:
        parser.error("set JAZZX_TOKEN or pass --token")
    if not args.inspect and not args.dry_run and not (args.target_collection_id or args.create_project):
        parser.error("provide --target-collection-id or --create-project unless --inspect is used")
    loan_number = args.loan_number.strip()
    if not loan_number or len(loan_number) > 128 or not re.fullmatch(r"[A-Za-z0-9_.-]+", loan_number):
        parser.error("--loan-number must contain only letters, numbers, dot, underscore, or hyphen")
    try:
        source_collection_id = validated_uuid(args.source_collection_id, "--source-collection-id")
        target_collection_id = validated_uuid(args.target_collection_id, "--target-collection-id") if args.target_collection_id else None
        client = JazzXClient(args.token, args.user_session_id, args.gateway_url, args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    entities = client.entities(source_collection_id)
    if not entities:
        raise RuntimeError("No entities found in the source collection")
    if args.inspect:
        by_type = grouped(entities)
        print(json.dumps({
            "source_collection_id": source_collection_id,
            "entity_count": len(entities),
            "entity_types": {entity_type: [{"id": entity.get("id"), "name": entity.get("name")} for entity in items]
                             for entity_type, items in sorted(by_type.items())},
        }, indent=2))
        return 0
    document = reconstruct(loan_number, entities)
    validator = load_validator()
    result = validator.validate_input(document)
    validator.print_results(result)
    if result.has_errors:
        raise RuntimeError("Reconstructed JSON failed validation; nothing was uploaded")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    if args.dry_run:
        print(json.dumps({
            "source_collection_id": source_collection_id,
            "entity_count": len(entities),
            "output": str(args.output) if args.output else None,
            "dry_run": True,
        }, indent=2))
        return 0
    project: dict[str, Any] | None = None
    if args.create_project:
        project_name = (args.project_name or loan_number).strip()
        if not project_name or any(character in project_name for character in "\r\n"):
            parser.error("project name cannot be blank or contain newlines")
        project = client.create_project(project_name, document.get("project_description") or f"Reconstructed loan {loan_number}")
        target_collection_id = validated_uuid(str(project["collection_id"]), "created project collection_id")
    # Knowledge Hub currently rejects a .json upload even though the content is
    # valid JSON. A .txt document is supported and preserves the exact JSON
    # payload so it can be downloaded and used as input again. The timestamp
    # and random suffix ensure every run creates a distinct document.
    created_suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{loan_number}_{created_suffix}_{uuid.uuid4().hex[:8]}.txt"
    uploaded = client.upload_json(target_collection_id, filename, document)
    print(json.dumps({"source_collection_id": source_collection_id, "target_collection_id": target_collection_id,
                      "project_id": project.get("id") if project else None,
                      "project_name": project.get("name") if project else None,
                      "entity_count": len(entities), "filename": filename, "upload_response": uploaded}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
