#!/usr/bin/env python3
"""Clone every Knowledge Hub entity for a JazzX loan into a new project.

The reference loan is identified by loan number.  The script reads the
LoanProject from a dashboard collection, fetches all entities from that
loan's collection, creates a new project, then recreates its entities in the
new collection.  IDs returned by JazzX are substituted everywhere in
``json_value`` before dependent entities are created.

Examples:
  # Inspect what would be created (no API writes)
  python loan_clone.py --source-file source_entities.json --target-loan-number 1441010_1 --dry-run

  # Clone a live loan found in the dashboard collection
  JAZZX_TOKEN=... python loan_clone.py --reference-loan-number 1441010 \
    --target-loan-number 1441010_1 --dashboard-collection-id <dashboard-uuid>

  # When the source collection is already known
  JAZZX_TOKEN=... python loan_clone.py --reference-loan-number 1441010 \
    --target-loan-number 1441010_1 --source-collection-id <loan-collection-uuid>
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_GATEWAY_URL = os.getenv("JAZZX_GATEWAY_URL", "")
READ_ONLY_FIELDS = {"id", "created_at", "updated_at", "created_by", "updated_by"}
LOAN_NUMBER_KEYS = {"loan_number", "loanNumber", "external_loan_number", "externalLoanNumber"}
PROJECT_ID_KEYS = {"external_loan_id", "externalLoanId", "loan_id", "loanId", "project_id", "projectId"}
COLLECTION_ID_KEYS = {"collection_id", "collectionId"}
ENUMS = {
    "loan_purpose": {"Purchase", "RefinanceRateAndTerm", "RefinanceCashOut", "RefinanceNoCashOut", "RefinanceLimitedCashOut", "Construction", "ConstructionToPermanent", "HomeImprovement", "Other"},
    "loan_type": {"Conventional", "Conv", "FHA", "VA", "USDA", "Jumbo", "SuperJumbo", "NonQM", "ReverseMortgage", "PortfolioProduct", "HELOCFirstLien", "HELOCSecondLien"},
    "mortgage_type": {"Conventional", "Conv", "FHA", "VA", "USDA", "Jumbo", "SuperJumbo", "NonQM", "ReverseMortgage", "PortfolioProduct", "HELOCFirstLien", "HELOCSecondLien"},
    "lien_type": {"FirstLien", "SecondLien", "ThirdLien", "HELOC"},
    "loan_status": {"Application", "Processing", "Submitted", "Underwriting", "ConditionalApproval", "Approved", "ClearToClose", "ClosingScheduled", "Docs", "Funding", "Funded", "Purchased", "Denied", "Withdrawn", "Cancelled", "Suspended"},
    "amortization_type": {"Fixed", "FixedRateMortgage", "AdjustableRate", "GraduatedPaymentMortgage", "GrowingEquityMortgage", "InterestOnly", "Balloon", "NegativeAmortization", "Step", "BuydownTemporary", "BuydownPermanent"},
    "loan_amortization_type": {"FixedRateMortgage", "AdjustableRate", "GraduatedPaymentMortgage", "GrowingEquityMortgage", "InterestOnly", "Balloon", "NegativeAmortization", "Step", "BuydownTemporary", "BuydownPermanent"},
    "current_loan_stage": {"Application", "Processing", "Submitted", "Underwriting", "ConditionalApproval", "Approved", "ClearToClose", "ClosingScheduled", "Docs", "Funding", "Funded", "Purchased", "Denied", "Withdrawn", "Cancelled", "Suspended"},
    "asset_type": {"CheckingAccount", "SavingsAccount", "MoneyMarket", "CertificateOfDeposit", "MutualFund", "Stock", "Bond", "RetirementAccount", "IRA", "401k", "403b", "PensionFund", "TrustFund", "LifeInsuranceCashValue", "StockOptions", "BridgeLoanProceeds", "IndividualDevelopmentAccount", "CashOnHand", "GiftFunds", "Grant", "RealEstateEquity", "SecuredBorrowedFunds", "UnsecuredBorrowedFunds", "SaleOfChattel", "TradeEquity", "SweatEquity", "CashDepositOnSalesContract", "RelocationFunds", "EmployerAssistedHousing", "LeasePurchaseFund", "LotEquity", "RentWithOptionToPurchase", "OtherLiquidAssets", "OtherNonLiquidAssets", "ProceedsFromSaleOfHome", "ProceedsFromSecuredLoan", "ProceedsFromUnsecuredLoan", "EarnestMoneyDeposit"},
    "income_type": {"Base", "Overtime", "Bonus", "Commission", "MilitaryBasePay", "MilitaryRationsAllowance", "MilitaryFlightPay", "MilitaryHazardPay", "MilitaryClothesAllowance", "MilitaryQuartersAllowance", "MilitaryPropPay", "MilitaryOverseasPay", "MilitaryCombatPay", "MilitaryVariableHousingAllowance", "SelfEmployment", "SocialSecurity", "Pension", "Retirement", "Disability", "ChildSupport", "Alimony", "RentalIncome", "InterestDividends", "NotesReceivable", "Trust", "OtherIncome", "AutomobileAllowance", "BoarderIncome", "CapitalGains", "EmploymentRelatedAssets", "FosterCare", "HousingAllowance", "MortgageCreditCertificate", "MortgageDifferential", "PublicAssistance", "RoyaltyPayment", "SeasonalIncome", "SecondaryEmployment", "TemporaryLeave", "TipIncome", "UnemploymentBenefits", "VABenefits", "AccessoryUnitIncome", "Employment"},
    "income_frequency": {"Weekly", "BiWeekly", "SemiMonthly", "Monthly", "Quarterly", "SemiAnnually", "Annually"},
    "documentation_level": {"FullDocumentation", "ReducedDocumentation", "NoDocumentation", "Stated"},
    "verification_status": {"NotVerified", "VerbalVOE", "WrittenVOE", "PaystubVerified", "TaxReturnVerified", "BankStatementVerified"},
    "liability_type": {"Mortgage", "HELOC", "Installment", "Revolving", "OpenThirtyDay", "LeasePayment", "ChildSupport", "Alimony", "SeparateMaintenanceExpense", "JobRelatedExpense", "Other", "CollectionsJudgments", "DeferredStudentLoan", "GovernmentStudentLoan", "Taxes", "MedicalDebt", "AutoLoan", "StudentLoan", "PersonalLoan"},
    "property_type": {"SingleFamily", "Single Family", "Condominium", "Townhouse", "Cooperative", "TwoToFourUnit", "ManufacturedSingleWide", "ManufacturedDoubleWide", "ManufacturedMultiWide", "PUD", "Modular", "MixedUse", "DetachedCondominium", "HighRiseCondominium"},
    "occupancy_type": {"PrimaryResidence", "SecondHome", "Investment"},
    "intended_usage_type": {"PrimaryResidence", "SecondHome", "Investment"},
    "construction_type": {"Existing", "NewConstruction", "ConstructionToPermanent", "Proposed"},
    "manufactured_width_type": {"SingleWide", "DoubleWide", "MultiWide"},
    "condo_project_classification": {"Established", "NewProject", "TwoToFourUnitProject", "DetachedCondo", "Manufactured", "NonWarrantable"},
    "title_manner_held": {"JointTenants", "TenantsInCommon", "CommunityProperty", "SoleOwnership", "Trust", "LLC", "Corporation", "Partnership", "LifeEstate"},
    "citizenship_type": {"USCitizen", "PermanentResidentAlien", "NonPermanentResidentAlien"},
    "marital_status": {"Married", "Separated", "Unmarried"},
    "suffix": {"Jr", "Sr", "II", "III", "IV"},
    "hmda_ethnicity": {"HispanicOrLatino", "NotHispanicOrLatino", "InformationNotProvided", "NotApplicable"},
    "hmda_race": {"AmericanIndianOrAlaskaNative", "Asian", "BlackOrAfricanAmerican", "NativeHawaiianOrOtherPacificIslander", "White", "InformationNotProvided", "NotApplicable"},
    "hmda_sex": {"Female", "Male", "InformationNotProvided", "NotApplicable"},
    "employment_status": {"Employed", "SelfEmployed", "Retired", "NotEmployed", "Military", "IndependentContractor"},
    "employment_type": {"Current", "Previous", "Secondary", "W2 Employed", "W-2 Employed", "Self Employed"},
    "employment_classification_type": {"Primary", "Current", "Previous", "Secondary"},
    "credit_bureau": {"Equifax", "Experian", "TransUnion"},
    "bureau": {"Equifax", "Experian", "TransUnion"},
    "report_type": {"Individual", "Joint", "TriMerge", "SingleBureau"},
    "credit_report_type": {"Individual", "Joint", "TriMerge", "SingleBureau"},
    "credit_model_type": {"FICO8", "FICO9", "FICO10", "FICO10T", "FICOAuto", "FICOBankcard", "VantageScore3", "VantageScore4", "ClassicFICO", "FICO Classic v5", "FICO Classic 04", "FICO Classic 98", "FICO Score 8", "FICO Score 9", "FICO Score 10"},
}
US_STATE_CODES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "PR", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "VI", "WA", "WV", "WI", "WY", "GU", "AS", "MP"}


def validate_uuid(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID, got {value!r}") from exc


class JazzXClient:
    """Small stdlib-only client for the two JazzX APIs used by this workflow."""

    def __init__(self, gateway_url: str, token: str | None, user_session_id: str | None,
                 user_agent: str = "JazzX-Loan-Clone-Automation/1.0", timeout: float = 60):
        if not token:
            raise ValueError("A JazzX token is required. Pass --token or set JAZZX_TOKEN.")
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
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": user_agent}
        self.project_headers = {
            **self.headers,
            "Content-Type": "application/json",
        }
        if user_session_id:
            self.project_headers["x-user-session-id"] = user_session_id

    def request(self, method: str, path: str, *, payload: Any = None, query: dict[str, Any] | None = None,
                project_api: bool = False) -> Any:
        url = f"{self.gateway_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = dict(self.project_headers if project_api else self.headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                try:
                    return json.loads(raw) if raw else {}
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{method} {path} returned invalid JSON") from exc
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"{method} {path} failed ({exc.code}): {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"{method} {path} failed: {getattr(exc, 'reason', exc)}") from exc

    def entities_in_collection(self, collection_id: str) -> list[dict[str, Any]]:
        """Fetch all pages; both list and {items/data: []} responses are accepted."""
        entities: list[dict[str, Any]] = []
        seen: set[str] = set()
        seen_pages: set[tuple[str, ...]] = set()
        skip = 0
        while True:
            page = self.request("GET", "/knowledge_hub/api/v1/reasoning/entities", query={
                "odata_query": f"collection_id eq '{collection_id}'", "skip": skip, "limit": 1000,
                "orderby": "created_at", "order_direction": "desc",
            })
            if isinstance(page, list):
                items = page
            elif isinstance(page, dict):
                items = page.get("items") or page.get("data") or []
            else:
                raise RuntimeError("entity API returned an invalid list envelope")
            if not isinstance(items, list) or not items:
                break
            signature = tuple(str(item.get("id") or "") for item in items if isinstance(item, dict))
            if signature in seen_pages:
                raise RuntimeError("entity pagination repeated a page; refusing an infinite loop")
            seen_pages.add(signature)
            for entity in items:
                if not isinstance(entity, dict):
                    continue
                entity = deepcopy(entity)
                if isinstance(entity.get("json_value"), str):
                    try:
                        entity["json_value"] = json.loads(entity["json_value"])
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"entity {entity.get('id')} contains invalid serialized json_value"
                        ) from exc
                entity_id = str(entity.get("id", ""))
                if entity_id and entity_id in seen:
                    continue
                if entity_id:
                    seen.add(entity_id)
                entities.append(entity)
            if len(items) < 1000:
                break
            skip += 1000
        return entities

    def active_projects_named(self, name: str) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.request(
                "GET", "/assistant/api/v1/projects", query={"limit": 100, "offset": offset}, project_api=True
            )
            if isinstance(page, list):
                items = page
            elif isinstance(page, dict):
                items = page.get("items") or page.get("data") or []
            else:
                raise RuntimeError("project API returned an invalid list envelope")
            if not isinstance(items, list):
                raise RuntimeError("project API returned an invalid list envelope")
            projects.extend(
                item for item in items if isinstance(item, dict)
                and item.get("name") == name
                and str(item.get("status", "open")).lower() != "archived"
            )
            if len(items) < 100:
                return projects
            offset += len(items)

    def create_project(self, project_name: str, description: str) -> dict[str, Any]:
        matches = self.active_projects_named(project_name)
        if matches:
            ids = ", ".join(str(item.get("id")) for item in matches)
            raise RuntimeError(f"Active project {project_name!r} already exists ({ids}); refusing a duplicate")
        return self.request("POST", "/assistant/api/v1/project/resources", payload={
            "name": project_name, "description": description,
            "image_url": "/images/project/folderImages/teal-folder.svg", "display_color": "teal",
        }, project_api=True)

    def create_entity(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/knowledge_hub/api/v1/reasoning/entities", payload=payload)

    def patch_entity_json(self, entity_id: str, json_value: dict[str, Any]) -> dict[str, Any]:
        # JazzX's documented update endpoint takes {"json_value": {...}}.
        return self.request("PATCH", f"/knowledge_hub/api/v1/reasoning/entities/{entity_id}", payload={"json_value": json_value})


def nested_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from nested_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested_values(item)
    else:
        yield value


def replace_ids(value: Any, id_mapping: dict[str, str], unresolved: dict[str, str] | None = None) -> Any:
    """Deep-copy and replace source IDs.  ``unresolved`` preserves initial create validity."""
    if isinstance(value, dict):
        return {key: replace_ids(item, id_mapping, unresolved) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_ids(item, id_mapping, unresolved) for item in value]
    if isinstance(value, str) and value in id_mapping:
        return id_mapping[value]
    if isinstance(value, str) and unresolved is not None and value in unresolved:
        return unresolved[value]
    return value


def source_dependencies(entity: dict[str, Any], source_ids: set[str]) -> set[str]:
    own_id = str(entity.get("id", ""))
    return {value for value in nested_values(entity.get("json_value", {})) if isinstance(value, str)
            and value in source_ids and value != own_id}


def entity_order(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Topologically order known references, retaining source order for ties/cycles."""
    by_id = {str(entity["id"]): entity for entity in entities if entity.get("id")}
    pending = {entity_id: source_dependencies(entity, set(by_id)) for entity_id, entity in by_id.items()}
    ordered: list[dict[str, Any]] = []
    while pending:
        ready = [entity_id for entity_id, needs in pending.items() if not needs]
        if not ready:  # A real cycle is fixed after creation through PATCH.
            ordered.extend(by_id[entity_id] for entity_id in pending)
            break
        for entity_id in ready:
            ordered.append(by_id[entity_id])
            pending.pop(entity_id)
        completed = set(ready)
        for needs in pending.values():
            needs.difference_update(completed)
    # Entities without an id cannot be relationship targets, but still clone them.
    ordered.extend(entity for entity in entities if not entity.get("id"))
    return ordered


def rewrite_loan_identity(value: Any, loan_number: str, project_id: str, collection_id: str) -> Any:
    """Update the identifiers that must belong to the newly created loan."""
    if isinstance(value, dict):
        result = {key: rewrite_loan_identity(item, loan_number, project_id, collection_id) for key, item in value.items()}
        for key in LOAN_NUMBER_KEYS.intersection(result):
            result[key] = loan_number
        for key in PROJECT_ID_KEYS.intersection(result):
            result[key] = project_id
        for key in COLLECTION_ID_KEYS.intersection(result):
            result[key] = collection_id
        return result
    if isinstance(value, list):
        return [rewrite_loan_identity(item, loan_number, project_id, collection_id) for item in value]
    return value


def clone_payload(source: dict[str, Any], collection_id: str, loan_number: str, project_id: str,
                  id_mapping: dict[str, str], placeholder_ids: dict[str, str]) -> dict[str, Any]:
    payload = {key: deepcopy(value) for key, value in source.items() if key not in READ_ONLY_FIELDS}
    payload["collection_id"] = collection_id
    payload["name"] = f"{loan_number}_{source.get('entity_type', 'Entity')}_{uuid.uuid4().hex[:8]}"
    payload["json_value"] = rewrite_loan_identity(
        replace_ids(payload.get("json_value", {}), id_mapping, placeholder_ids), loan_number, project_id, collection_id
    )
    return payload


def validate_entities(entities: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    ids = [str(entity.get("id")) for entity in entities if entity.get("id")]
    duplicate_ids = sorted({entity_id for entity_id in ids if ids.count(entity_id) > 1})
    if duplicate_ids:
        problems.append(f"duplicate entity IDs are not safe to remap: {duplicate_ids}")
    for index, entity in enumerate(entities):
        label = f"entities[{index}]"
        if not entity.get("id"):
            problems.append(f"{label}.id is required to safely remap relationships")
        if not entity.get("entity_type"):
            problems.append(f"{label}.entity_type is required")
        if "json_value" in entity and not isinstance(entity["json_value"], dict):
            problems.append(f"{label}.json_value must be an object")
    return problems


def semantic_warnings(entities: list[dict[str, Any]]) -> list[str]:
    """Non-destructive checks adapted from validate_input for cloned entity JSON.

    They intentionally validate only fields that are present: source entities can
    legitimately come from different ontology versions, while malformed values
    must still be visible before a duplicate is created.
    """
    warnings: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                field = key.replace("-", "_").replace(" ", "_")
                field = re.sub(r"(?<!^)(?=[A-Z])", "_", field).lower()
                item_path = f"{path}.{key}"
                allowed = ENUMS.get(field)
                if allowed and item not in (None, ""):
                    if isinstance(item, list):
                        invalid = [entry for entry in item if entry not in (None, "") and entry not in allowed]
                        if invalid:
                            warnings.append(f"{item_path}: invalid enum values {invalid!r}")
                    elif item not in allowed:
                        warnings.append(f"{item_path}: invalid enum value {item!r}")
                if isinstance(item, float) and (math.isnan(item) or math.isinf(item)):
                    warnings.append(f"{item_path}: must be a finite number")
                # ``state`` is also used by workflow/classification entities
                # (e.g. state=CLASSIFIED).  The input validator applies these
                # rules only inside an address object.
                address_context = any(token in path.lower() for token in ("address", "residence", "property"))
                if address_context and field in {"state", "state_code"} and item and item not in US_STATE_CODES:
                    warnings.append(f"{item_path}: invalid US state code {item!r}")
                if address_context and field in {"postal_code", "zip_code"} and item and (not isinstance(item, str) or not re.fullmatch(r"[0-9]{5}(-[0-9]{4})?", item)):
                    warnings.append(f"{item_path}: invalid postal code")
                if "email" in field and item and (not isinstance(item, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", item)):
                    warnings.append(f"{item_path}: invalid email")
                if field.endswith("date") and item and isinstance(item, str) and "T" not in item:
                    try:
                        from datetime import datetime
                        datetime.strptime(item, "%Y-%m-%d")
                    except ValueError:
                        warnings.append(f"{item_path}: expected YYYY-MM-DD date")
                if any(token in field for token in ("percent", "ratio")) and isinstance(item, (int, float)) and not isinstance(item, bool) and not 0 <= item <= 100:
                    warnings.append(f"{item_path}: percentage/ratio must be between 0 and 100")
                if field in {"score", "credit_score"} and isinstance(item, int) and not isinstance(item, bool) and not 300 <= item <= 850:
                    warnings.append(f"{item_path}: credit score must be between 300 and 850")
                if field in {"dependent_ages", "dependentages"} and isinstance(item, list) and any(not isinstance(age, int) or isinstance(age, bool) for age in item):
                    warnings.append(f"{item_path}: dependent ages must be integers")
                walk(item, item_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    for index, entity in enumerate(entities):
        walk(entity.get("json_value", {}), f"entities[{index}].json_value")
    return warnings


def json_contains(actual: Any, expected: Any) -> bool:
    """True when JazzX's returned JSON contains the exact payload we sent.

    JazzX may add metadata, so this deliberately permits extra dictionary keys.
    """
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and json_contains(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(json_contains(a, e) for a, e in zip(actual, expected))
    return actual == expected


def verify_clone(client: JazzXClient, collection_id: str, expected: list[dict[str, Any]], source_ids: set[str],
                 expected_json: dict[str, dict[str, Any]]) -> list[str]:
    """Read the target back and validate every created entity's persisted JSON."""
    actual = client.entities_in_collection(collection_id)
    failures: list[str] = []
    by_target_id = {str(entity.get("id")): entity for entity in actual}
    for item in expected:
        target = by_target_id.get(item["target_id"])
        if target is None:
            failures.append(f"created {item['entity_type']} {item['target_id']} was not returned by target collection")
            continue
        if target.get("collection_id") != collection_id:
            failures.append(f"target entity {item['target_id']} is in the wrong collection")
        desired_json = expected_json[item["target_id"]]
        if not json_contains(target.get("json_value"), desired_json):
            failures.append(f"target entity {item['target_id']} JSON differs from its validated clone payload")
        target_warnings = semantic_warnings([target])
        if target_warnings:
            failures.extend(f"target entity {item['target_id']}: {warning}" for warning in target_warnings)
    for entity in actual:
        old_ids = {value for value in nested_values(entity.get("json_value", {})) if isinstance(value, str) and value in source_ids}
        if old_ids:
            failures.append(f"target entity {entity.get('id')} still contains source IDs: {sorted(old_ids)}")
    return failures


def find_source_collection(client: JazzXClient, reference_number: str, dashboard_collection_id: str) -> tuple[str, dict[str, Any]]:
    candidates = client.entities_in_collection(dashboard_collection_id)
    for entity in candidates:
        if entity.get("entity_type") != "LoanProject":
            continue
        value = entity.get("json_value") or {}
        matches = {str(value.get(key, "")) for key in LOAN_NUMBER_KEYS}
        if reference_number in matches or str(entity.get("name", "")) == reference_number:
            collection_id = value.get("collection_id")
            if collection_id:
                return str(collection_id), entity
            raise RuntimeError(f"LoanProject for {reference_number} has no json_value.collection_id")
    raise RuntimeError(f"No LoanProject matching reference loan number {reference_number} was found")


def read_source_file(path: Path) -> tuple[str | None, list[dict[str, Any]]]:
    if not path.is_file():
        raise ValueError(f"source file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        collection_id, entities = None, payload
    elif isinstance(payload, dict) and isinstance(payload.get("entities"), list):
        collection_id, entities = payload.get("collection_id"), payload["entities"]
    else:
        raise ValueError("--source-file must be a JSON entity list or an object with an 'entities' list")
    normalized: list[dict[str, Any]] = []
    for index, entity in enumerate(entities):
        if not isinstance(entity, dict):
            raise ValueError(f"entities[{index}] must be an object")
        entity = deepcopy(entity)
        if isinstance(entity.get("json_value"), str):
            try:
                entity["json_value"] = json.loads(entity["json_value"])
            except json.JSONDecodeError as exc:
                raise ValueError(f"entities[{index}].json_value is not valid JSON") from exc
        normalized.append(entity)
    return collection_id, normalized


def main() -> int:
    parser = argparse.ArgumentParser(description="Clone a JazzX loan into a new project using one script.")
    parser.add_argument("--reference-loan-number", help="Existing loan number to clone")
    parser.add_argument("--target-loan-number", required=True, help="New loan number, e.g. 1441010_1")
    parser.add_argument("--source-collection-id", help="Known source loan collection ID")
    parser.add_argument("--dashboard-collection-id", help="Collection containing LoanProject entities")
    parser.add_argument("--source-file", type=Path, help="Offline source JSON for repeatable dry runs")
    parser.add_argument("--project-name", help="New Assistant project name (default: clone_<target loan>)")
    parser.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--user-session-id", default=os.getenv("JAZZX_USER_SESSION_ID"),
                        help="Current POC2 x-user-session-id, if your environment requires one")
    parser.add_argument("--user-agent", default=os.getenv("JAZZX_USER_AGENT", "JazzX-Loan-Clone-Automation/1.0"))
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--dry-run", action="store_true", help="Do not create or patch anything")
    parser.add_argument("--validate-only", action="store_true", help="Run source validation and exit without creating a project")
    parser.add_argument("--allow-validation-warnings", action="store_true", help="Create anyway when source JSON has validation warnings")
    parser.add_argument("--output", type=Path, help="Write the JSON result to this file")
    args = parser.parse_args()

    args.source_collection_id = validate_uuid(args.source_collection_id, "--source-collection-id")
    args.dashboard_collection_id = validate_uuid(args.dashboard_collection_id, "--dashboard-collection-id")

    if not args.target_loan_number.strip():
        parser.error("--target-loan-number cannot be blank")
    args.target_loan_number = args.target_loan_number.strip()
    if len(args.target_loan_number) > 128 or not re.fullmatch(r"[A-Za-z0-9_.-]+", args.target_loan_number):
        parser.error("--target-loan-number must contain only letters, numbers, dot, underscore, or hyphen")
    if args.reference_loan_number and args.reference_loan_number.strip() == args.target_loan_number:
        parser.error("target loan number must differ from the reference loan number")
    if not args.source_file and not args.reference_loan_number:
        parser.error("--reference-loan-number is required unless --source-file is supplied")
    if args.source_file:
        source_collection_id, entities = read_source_file(args.source_file)
        if source_collection_id:
            source_collection_id = validate_uuid(str(source_collection_id), "source-file collection_id")
    else:
        client = JazzXClient(args.gateway_url, args.token, args.user_session_id, args.user_agent, args.timeout)
        source_collection_id = args.source_collection_id
        dashboard_project = None
        if args.dashboard_collection_id:
            discovered_collection_id, dashboard_project = find_source_collection(
                client, args.reference_loan_number, args.dashboard_collection_id
            )
            discovered_collection_id = validate_uuid(
                discovered_collection_id, "LoanProject collection_id"
            )
            if source_collection_id and source_collection_id != discovered_collection_id:
                raise ValueError("--source-collection-id does not match the collection on the reference LoanProject")
            source_collection_id = discovered_collection_id
        elif not source_collection_id:
            parser.error("provide --source-collection-id or --dashboard-collection-id for a live clone")
        entities = client.entities_in_collection(source_collection_id)
        # LoanProject lives in the dashboard collection, not the loan collection.
        if dashboard_project and not any(entity.get("id") == dashboard_project.get("id") for entity in entities):
            entities.insert(0, dashboard_project)

    problems = validate_entities(entities)
    if problems:
        raise ValueError("Source validation failed:\n- " + "\n- ".join(problems))
    if not entities:
        raise ValueError("No source entities were found; refusing to create an empty project")
    warnings = semantic_warnings(entities)
    if warnings:
        print("SOURCE VALIDATION WARNINGS:\n- " + "\n- ".join(warnings), file=sys.stderr)
    if warnings and not args.allow_validation_warnings:
        raise ValueError("Source JSON validation failed; fix the values or explicitly use --allow-validation-warnings")
    if args.validate_only:
        print(json.dumps({"source_collection_id": source_collection_id, "entity_count": len(entities),
                          "validation_warnings": warnings, "valid": not warnings}, indent=2))
        return 0

    description = f"Clone of {args.reference_loan_number or source_collection_id}"
    if args.dry_run:
        project = {"id": f"dry-run-project-{args.target_loan_number}",
                   "collection_id": f"dry-run-collection-{args.target_loan_number}"}
        client = None
    else:
        client = JazzXClient(args.gateway_url, args.token, args.user_session_id, args.user_agent, args.timeout)
        project_name = (args.project_name or f"clone_{args.target_loan_number}").strip()
        if not project_name or len(project_name) > 255 or any(character in project_name for character in "\r\n"):
            parser.error("--project-name cannot be blank, exceed 255 characters, or contain newlines")
        project = client.create_project(project_name, description)
        if not project.get("collection_id"):
            raise RuntimeError(f"Project creation did not return collection_id: {project}")
        project["id"] = validate_uuid(str(project.get("id") or ""), "created project id")
        project["collection_id"] = validate_uuid(
            str(project["collection_id"]), "created project collection_id"
        )

    # Placeholders avoid accidental source IDs in circular references during POST.
    source_ids = {str(entity["id"]) for entity in entities}
    id_mapping: dict[str, str] = {}
    placeholders = {source_id: f"pending-reference-{uuid.uuid4()}" for source_id in source_ids}
    created: list[dict[str, Any]] = []
    original_json: dict[str, dict[str, Any]] = {}
    expected_json: dict[str, dict[str, Any]] = {}
    for source in entity_order(entities):
        source_id = str(source["id"])
        payload = clone_payload(source, project["collection_id"], args.target_loan_number, project["id"], id_mapping, placeholders)
        original_json[source_id] = deepcopy(source.get("json_value") or {})
        if args.dry_run:
            response = {"id": f"dry-run-{source_id}"}
        else:
            response = client.create_entity(payload)
        if not response.get("id"):
            raise RuntimeError(f"Creation of {source_id} returned no entity id: {response}")
        id_mapping[source_id] = str(response["id"])
        created.append({"source_id": source_id, "target_id": id_mapping[source_id], "entity_type": source.get("entity_type")})

    # Patch every entity from the source truth once all actual IDs are available.
    # This resolves cycles and guarantees no old ID survives in json_value.
    if not args.dry_run:
        for source_id, json_value in original_json.items():
            target_json = rewrite_loan_identity(
                replace_ids(json_value, id_mapping), args.target_loan_number, project["id"], project["collection_id"]
            )
            client.patch_entity_json(id_mapping[source_id], target_json)
            expected_json[id_mapping[source_id]] = target_json
    if args.dry_run:
        # This is the JSON that would be persisted after every reference is resolved.
        for source_id, json_value in original_json.items():
            expected_json[id_mapping[source_id]] = rewrite_loan_identity(
                replace_ids(json_value, id_mapping), args.target_loan_number, project["id"], project["collection_id"]
            )

    verification_failures = [] if args.dry_run else verify_clone(
        client, project["collection_id"], created, source_ids, expected_json
    )
    if verification_failures:
        raise RuntimeError("Clone verification failed:\n- " + "\n- ".join(verification_failures))

    result = {"source_collection_id": source_collection_id, "target_project_id": project.get("id"),
              "target_collection_id": project["collection_id"], "reference_loan_number": args.reference_loan_number,
              "target_loan_number": args.target_loan_number, "dry_run": args.dry_run,
              "created_count": len(created), "created_entities": created, "id_mapping": id_mapping,
              "validation_warnings": warnings, "verification": "skipped (dry run)" if args.dry_run else "passed"}
    encoded = json.dumps(result, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
