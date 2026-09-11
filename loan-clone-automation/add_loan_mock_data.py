#!/usr/bin/env python3
"""
Add Loan Mock Data Script (Refactored)

This script:
1. Creates a project in JazzX via the gateway API
2. Extracts the project id (used as loanId) and collection_id
3. Creates all Knowledge Hub entities using payload builders
4. (Optional) Generates mock data entries and pushes to mock server

Usage:
    python add_loan_mock_data.py --input loan_data.json
"""

import argparse
import json
import os
import random
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from pydantic import ValidationError as PydanticValidationError

# The payload-builder package is located at the workspace root beside this
# script's directory. This makes the script runnable from any working folder.
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

# Import payload builders from entity_creators package
from entity_creators import (
    LoanProjectPayloadBuilder,
    LoanCorePayloadBuilder,
    BorrowerPayloadBuilder,
    AssetPayloadBuilder,
    IncomePayloadBuilder,
    LiabilityPayloadBuilder,
    SubjectPropertyPayloadBuilder,
    EmploymentPayloadBuilder,
    CreditReportPayloadBuilder,
    LoanApplicationPayloadBuilder,
    LoanDetailsPayloadBuilder,
    generate_uuid,
)
from validate_input import print_results as print_validation_results
from validate_input import validate_input

# =============================================================================
# UUID Generation Helpers (for mock data)
# =============================================================================


def generate_condition_id() -> str:
    """Generate a condition-style UUID (prefixed with 'cond-')."""
    return f"cond-{uuid.uuid4()}"


def generate_objective_id() -> str:
    """Generate an objective-style UUID (prefixed with 'obj-')."""
    return f"obj-{uuid.uuid4()}"


def random_mock_id() -> int:
    """Generate a random integer ID for mock data entries."""
    return random.randint(1000, 99999)


# =============================================================================
# Default Configuration
# =============================================================================

DEFAULT_GATEWAY_URL = os.environ.get("JAZZX_GATEWAY_URL", "")
DEFAULT_TOKEN = ""
REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_ONTOLOGY_NAME = os.environ.get(
    "JAZZX_MORTGAGE_ONTOLOGY", "mortgage_ontology_v2"
)
DEFAULT_LOAN_PROJECT_ONTOLOGY_NAME = os.environ.get(
    "JAZZX_LOAN_PROJECT_ONTOLOGY", "loan_project"
)
DEFAULT_LOAN_DETAILS_ONTOLOGY_NAME = os.environ.get(
    "JAZZX_LOAN_DETAILS_ONTOLOGY", "loan_details"
)
DEFAULT_MOCK_DATA_PATH = os.environ.get(
    "JAZZX_MOCK_DATA_PATH", "/mock-server/api/mock-data"
)
DEFAULT_PIPELINE_PATH = os.environ.get("JAZZX_PIPELINE_PATH", "/pipeline")


def normalize_gateway_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value.startswith(("https://", "http://")):
        raise ValueError("gateway URL must start with https:// or http://")
    return value


def normalize_token(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value:
        raise ValueError("authentication token cannot be empty")
    return value


def validated_uuid(value: Optional[str], label: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{label} must be a UUID, got {value!r}") from error


def normalize_extracted_loan(data: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt the extracted snake_case loan format in memory.

    The extracted JSON remains unchanged on disk.  Entity builders consume the
    older camelCase master-input format, so this adapter translates only the
    fields needed by those builders.
    """
    if isinstance(data.get("loan"), dict) and data["loan"]:
        return data

    for field_name in ("loan_core", "loan_details", "subject_property", "project_details"):
        if data.get(field_name) is not None and not isinstance(data[field_name], dict):
            raise ValueError(f"{field_name} must be an object")
    if data.get("borrowers") is not None and not isinstance(data["borrowers"], list):
        raise ValueError("borrowers must be an array")

    core = data.get("loan_core") or {}
    details = data.get("loan_details") or {}
    summary = details.get("summary") or {}
    overview = details.get("loanOverview") or {}
    for field_name, field_value in (("loan_details.summary", summary), ("loan_details.loanOverview", overview)):
        if not isinstance(field_value, dict):
            raise ValueError(f"{field_name} must be an object")
    rate = overview.get("interestRateApr") or {}
    if not isinstance(rate, dict):
        raise ValueError("loan_details.loanOverview.interestRateApr must be an object")
    raw_property = data.get("subject_property") or {}
    property_info = details.get("propertyInformation") or {}
    if not isinstance(property_info, dict):
        raise ValueError("loan_details.propertyInformation must be an object")

    def addr(value: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("address values must be objects")
        return {
            "line": value.get("street_line_1") or value.get("line"),
            "city": value.get("city"),
            "stateCode": value.get("state") or value.get("stateCode"),
            "zipCode": value.get("postal_code") or value.get("zipCode") or value.get("zip"),
            "county": value.get("county"),
        }

    loan = {
        "loanNumber": core.get("loan_number") or summary.get("loanId"),
        "loanPurpose": core.get("loan_purpose", "Purchase"),
        "loanAmount": core.get("base_loan_amount_usd") or summary.get("loanAmount", 0),
        "currentLoanStage": core.get("loan_status") or summary.get("status", "Processing"),
        "createdAt": core.get("application_date") or overview.get("applicationDate"),
        "closingDate": summary.get("closingDate"),
        "loanOfficer": overview.get("loanOfficer"),
        "principalAndInterestPayment": overview.get("principalAndInterestPayment", 0),
        "loanProduct": {
            "lienType": "FirstLien" if core.get("lien_priority") == "First" else core.get("lien_priority"),
            "mortgageType": core.get("loan_type") or summary.get("loanType", "Conventional"),
            "loanAmortizationType": "FixedRateMortgage" if core.get("amortization_type") == "Fixed" else core.get("amortization_type"),
            "pricingEngineNoteRate": rate.get("interestRate", core.get("note_rate_percent", 0)),
            "apr": rate.get("apr", core.get("apr_percent", 0)),
            "loanTermMonthsCount": core.get("term_months", 0),
        },
        "ltvPercent": core.get("ltv_percent"),
    }
    ltv_ratio = overview.get("ltvRatio") or {}
    if not isinstance(ltv_ratio, dict):
        raise ValueError("loan_details.loanOverview.ltvRatio must be an object")
    if loan["ltvPercent"] is None:
        loan["ltvPercent"] = ltv_ratio.get("value")

    borrowers = []
    detail_borrowers = details.get("borrowerDetails") or []
    if not isinstance(detail_borrowers, list):
        raise ValueError("loan_details.borrowerDetails must be an array")
    for borrower_index, raw in enumerate(data.get("borrowers", [])):
        if not isinstance(raw, dict):
            raise ValueError(f"borrowers[{borrower_index}] must be an object")
        fields = raw.get("borrower_fields") or {}
        if not isinstance(fields, dict):
            raise ValueError(f"borrowers[{borrower_index}].borrower_fields must be an object")
        for list_name in ("assets", "liabilities", "employments", "income_sources"):
            values = raw.get(list_name) or []
            if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                raise ValueError(f"borrowers[{borrower_index}].{list_name} must be an array of objects")
        borrower = {
            "firstName": fields.get("first_name"), "middleName": fields.get("middle_name"),
            "lastName": fields.get("last_name"), "emailAddress": fields.get("email"),
            "cellPhoneNumber": fields.get("phone_cell") or fields.get("phone_home"),
            "dateOfBirth": fields.get("date_of_birth"), "ssnMasked": fields.get("ssn_masked"),
            "citizenshipType": fields.get("citizenship_status"), "maritalStatus": fields.get("marital_status"),
            "dependentCount": fields.get("dependent_count", 0),
            "currentAddress": addr(fields.get("current_address") or {}),
            "mailingAddressSameAsCurrent": True,
            "intentToOccupy": fields.get("will_occupy_as_primary_residence", True),
            "hmdaRace": fields.get("hmda_race", []), "hmdaEthnicity": fields.get("hmda_ethnicity", []),
            "hmdaSex": fields.get("hmda_sex"), "assets": [], "liabilities": [], "incomes": [],
            "declarations": {
                "ownershipInterestInPropertyLast3Years": fields.get("ownership_interest_in_property_last_3_years", False),
                "outstandingJudgments": fields.get("has_outstanding_judgments", False),
                "declaredBankruptcy": fields.get("has_declared_bankruptcy", False),
                "propertyForeclosed": fields.get("has_property_foreclosed", False),
                "partyToLawsuit": fields.get("is_party_to_lawsuit", False),
                "conveyedTitleInLieu": fields.get("has_conveyed_title_in_lieu", False),
                "preforeclosureShortSale": fields.get("has_preforeclosure_short_sale", False),
                "delinquentOnFederalDebt": fields.get("is_delinquent_on_federal_debt", False),
            },
        }
        # Preserve PDF-derived credit scores already present in loan_details.
        # The builders expect the master-input creditScores/medianCreditScore
        # shape, so translate it without recalculating or inventing values.
        if borrower_index < len(detail_borrowers):
            detail_borrower = detail_borrowers[borrower_index]
            if not isinstance(detail_borrower, dict):
                raise ValueError(f"loan_details.borrowerDetails[{borrower_index}] must be an object")
            profile = detail_borrower.get("borrowerProfile") or {}
            if not isinstance(profile, dict):
                raise ValueError(f"loan_details.borrowerDetails[{borrower_index}].borrowerProfile must be an object")
            profile_score = profile.get("creditScore") or {}
            if not isinstance(profile_score, dict):
                raise ValueError(f"loan_details.borrowerDetails[{borrower_index}].borrowerProfile.creditScore must be an object")
            breakdown = profile_score.get("bureauBreakdown") or {}
            if not isinstance(breakdown, dict):
                raise ValueError(f"loan_details.borrowerDetails[{borrower_index}].borrowerProfile.creditScore.bureauBreakdown must be an object")
            scores = []
            for bureau, key in (("Equifax", "equifax"), ("Experian", "experian"), ("TransUnion", "transUnion")):
                if breakdown.get(key) is not None:
                    scores.append({"bureau": bureau, "score": breakdown[key]})
            if scores:
                borrower["creditScores"] = scores
            if profile_score.get("representative") is not None:
                borrower["medianCreditScore"] = {"score": profile_score["representative"]}
        for item in raw.get("assets", []):
            borrower["assets"].append({
                "assetType": item.get("asset_type"), "institutionName": item.get("institution_name"),
                "accountNumber": item.get("account_number_masked"), "currentBalance": item.get("current_balance_usd"),
            })
        for item in raw.get("liabilities", []):
            borrower["liabilities"].append({
                "liabilityType": item.get("liability_type"), "creditorName": item.get("creditor_name"),
                "accountNumber": item.get("account_number_masked"), "unpaidBalance": item.get("unpaid_balance_usd"),
                "monthlyPaymentAmount": item.get("monthly_payment_usd"), "monthsRemaining": item.get("months_remaining"),
                "willBePaidOffAtClosing": item.get("will_be_paid_off_at_closing", False),
                "isExcludedFromDti": item.get("is_excluded_from_dti", False),
            })
        employment = next(iter(raw.get("employments", [])), None)
        employer = None
        if employment:
            employer = {
                "employmentClassificationType": employment.get("employment_type", "Current"),
                "status": employment.get("employment_type", "Current"), "name": employment.get("employer_name"),
                "jobTitle": employment.get("position_title"), "startDate": employment.get("start_date"),
                "numberOfMonthsInThisLineOfWork": employment.get("months_on_job"),
                "phone": employment.get("employer_phone"), "isSelfEmployed": employment.get("is_self_employed", False),
                "employmentStatus": "SelfEmployed" if employment.get("is_self_employed") else "Employed",
                "address": addr(employment.get("employer_address") or {}),
            }
        for income in raw.get("income_sources", []):
            amount = income.get("monthly_amount_usd")
            borrower["incomes"].append({
                "incomeType": income.get("income_type", "Base"),
                "totalCalculatedQualifiedMonthlyIncome": amount,
                "totalCalculatedStatedMonthlyIncome": amount,
                "frequency": "Monthly", "isPrimary": True,
                "isEmploymentIncome": bool(employer), "employer": employer,
            })
        borrowers.append(borrower)

    subject_property = {
        "address": addr(raw_property.get("address") or {}),
        "propertyType": raw_property.get("property_type") or property_info.get("propertyType", "SingleFamily"),
        "intendedUsageType": raw_property.get("occupancy_type") or property_info.get("occupancy", "PrimaryResidence"),
        "numberOfUnits": raw_property.get("number_of_units", 1),
        "yearBuilt": raw_property.get("year_built"),
        "estimatedValueAmount": raw_property.get("estimated_value_usd") or property_info.get("appraisalValue"),
        "appraisedValue": raw_property.get("appraised_value_usd") or property_info.get("appraisalValue"),
        "purchasePrice": raw_property.get("purchase_price_usd") or property_info.get("purchasePrice"),
    }
    return {"project_name": (data.get("project_details") or {}).get("project_number"),
            "project_description": "Auto-generated loan", "loan": loan,
            "borrowers": borrowers, "subject_property": subject_property}


# =============================================================================
# Project Creator
# =============================================================================


class ProjectCreator:
    """Creates a project via the JazzX gateway API."""

    def __init__(
        self, gateway_url: str = DEFAULT_GATEWAY_URL, token: str = DEFAULT_TOKEN
    ):
        self.gateway_url = normalize_gateway_url(gateway_url)
        self.project_endpoint = f"{self.gateway_url}/assistant/api/v1/project/resources"
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

    def create_project(self, name: str, description: str = ".") -> Dict[str, Any]:
        """Create a project and return the response with id and collection_id."""
        payload = {
            "name": name,
            "description": description,
            "image_url": "/images/project/folderImages/teal-folder.svg",
            "display_color": "teal",
        }

        try:
            print(f"🏗️  Creating project '{name}'...")
            response = requests.post(
                self.project_endpoint,
                json=payload,
                headers=self.headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not data.get("id"):
                raise ValueError(f"project creation returned an invalid response: {data!r}")
            print(f"   ✅ Project created: {data.get('id')}")
            print(f"   📁 Collection ID: {data.get('collection_id')}")
            return data
        except requests.exceptions.RequestException as e:
            print(f"❌ Error creating project: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"   Response: {e.response.text}")
            raise RuntimeError(f"Could not create project {name!r}") from e

    def get_or_create_project(
        self,
        name: str,
        description: str = ".",
        fallback_collection_id: Optional[str] = None,
        require_collection_id: bool = True,
        create_if_absent: bool = True,
    ) -> Dict[str, Any]:
        """Reuse an existing non-archived project, creating only when absent.

        The lookup is deliberately fail-closed.  If the project list cannot be
        read, creating a project could produce a duplicate that is not visible
        in the mortgage-app list.
        """
        try:
            projects: list[Dict[str, Any]] = []
            offset = 0
            seen_pages: set[tuple[str, ...]] = set()
            while True:
                response = requests.get(
                    f"{self.gateway_url}/assistant/api/v1/projects",
                    params={"limit": 100, "offset": offset},
                    headers=self.headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                page = response.json()
                page = page if isinstance(page, list) else page.get("items") or page.get("data") or []
                if not isinstance(page, list):
                    raise ValueError("project API returned an invalid list envelope")
                signature = tuple(str(item.get("id") or "") for item in page if isinstance(item, dict))
                if signature in seen_pages:
                    raise ValueError("project pagination repeated a page")
                seen_pages.add(signature)
                projects.extend(item for item in page if isinstance(item, dict))
                if len(page) < 100:
                    break
                offset += len(page)
            matches = [
                project for project in projects
                if project.get("name") == name
                and str(project.get("status", "open")).lower() != "archived"
            ]
            if len(matches) > 1:
                ids = ", ".join(str(project.get("id")) for project in matches)
                raise RuntimeError(
                    f"Multiple active projects are named {name!r}: {ids}; "
                    "use a unique --project-name"
                )
            for project in matches:
                    if (
                        require_collection_id
                        and not project.get("collection_id")
                        and project.get("id")
                    ):
                        detail = requests.get(
                            f"{self.gateway_url}/assistant/api/v1/project/{project['id']}",
                            headers=self.headers,
                            timeout=REQUEST_TIMEOUT_SECONDS,
                        )
                        detail.raise_for_status()
                        detail_data = detail.json()
                        if not isinstance(detail_data, dict):
                            raise ValueError("project detail API returned a non-object response")
                        project = {**project, **detail_data}
                    if require_collection_id and not project.get("collection_id"):
                        if fallback_collection_id:
                            project = {**project, "collection_id": fallback_collection_id}
                        else:
                            raise RuntimeError(
                                f"Existing project {project.get('id')} has no collection_id; "
                                "pass --collection-id to identify its Knowledge Hub collection"
                            )
                    print(f"♻️  Reusing existing project '{name}': {project.get('id')}")
                    print(f"   📁 Collection ID: {project.get('collection_id')}")
                    if (
                        fallback_collection_id
                        and project.get("collection_id")
                        and str(project["collection_id"]) != fallback_collection_id
                    ):
                        raise RuntimeError(
                            "--collection-id does not match the collection attached "
                            f"to project {project.get('id')}"
                        )
                    return project
        except (requests.exceptions.RequestException, ValueError) as e:
            detail = str(e)
            if getattr(e, "response", None) is not None:
                detail = f"{detail}; response={e.response.text[:500]}"
            raise RuntimeError(
                "Could not verify whether the project already exists; "
                f"refusing to create a duplicate project: {detail}"
            ) from e
        if not create_if_absent:
            raise RuntimeError(
                f"No active project named {name!r} exists; pipeline-only mode "
                "will not create a new project."
            )
        return self.create_project(name=name, description=description)


# =============================================================================
# Entity API - Common entity creation interface
# =============================================================================


class EntityAPI:
    """Common API for creating entities in the Knowledge Hub."""

    IDENTITY_FIELDS = {
        "Asset": ("borrower_id", "asset_type", "account_number_masked", "institution_name"),
        "IncomeSource": ("borrower_id", "income_type", "is_employment_income", "is_primary"),
        "Liability": ("borrower_id", "liability_type", "account_number_masked", "creditor_name"),
        "EmploymentRecord": ("employment_type", "employer_name"),
    }

    def __init__(
        self,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        token: str = DEFAULT_TOKEN,
        ontology_name: str = DEFAULT_ONTOLOGY_NAME,
        loan_project_ontology_name: str = DEFAULT_LOAN_PROJECT_ONTOLOGY_NAME,
        loan_details_ontology_name: str = DEFAULT_LOAN_DETAILS_ONTOLOGY_NAME,
    ):
        self.gateway_url = normalize_gateway_url(gateway_url)
        self.entity_endpoint = (
            f"{self.gateway_url}/knowledge_hub/api/v1/reasoning/entities"
        )
        self.ontology_endpoint = (
            f"{self.gateway_url}/knowledge_hub/api/v1/reasoning/ontologies"
        )
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._collection_entities: Dict[str, list[Dict[str, Any]]] = {}
        self._ontology_ids: Dict[str, str] = {}
        self.default_ontology_name = ontology_name
        self.ontology_name_by_entity_type = {
            "LoanProject": loan_project_ontology_name,
            "LoanDetails": loan_details_ontology_name,
        }

    def _resolve_ontology_id(self, ontology_name: str) -> str:
        """Resolve an environment-specific ontology UUID by its stable name."""
        if ontology_name in self._ontology_ids:
            return self._ontology_ids[ontology_name]

        escaped_name = ontology_name.replace("'", "''")
        try:
            response = requests.get(
                self.ontology_endpoint,
                params={
                    "odata_query": f"name eq '{escaped_name}'",
                    "limit": 10,
                },
                headers=self.headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.exceptions.RequestException, ValueError) as error:
            detail = str(error)
            if getattr(error, "response", None) is not None:
                detail = f"{detail}; response={error.response.text[:500]}"
            raise RuntimeError(
                f"Could not resolve ontology '{ontology_name}' from "
                f"{self.gateway_url}: {detail}"
            ) from error

        if isinstance(body, list):
            items = body
        elif isinstance(body, dict):
            items = body.get("items") or body.get("data") or body.get("results") or []
        else:
            raise RuntimeError(f"Ontology API returned {type(body).__name__}, expected list/object")
        if not isinstance(items, list):
            raise RuntimeError("Ontology API returned an invalid list envelope")
        matches = [item for item in items if item.get("name") == ontology_name]
        if not matches:
            raise RuntimeError(
                f"Ontology '{ontology_name}' is not deployed in {self.gateway_url}. "
                "Deploy the ontology before creating loan entities."
            )
        if len(matches) > 1:
            raise RuntimeError(
                f"Multiple ontologies named '{ontology_name}' exist in "
                f"{self.gateway_url}; refusing to choose an ambiguous ID."
            )

        ontology_id = matches[0].get("id")
        if not ontology_id:
            raise RuntimeError(
                f"Ontology '{ontology_name}' returned no ID from {self.gateway_url}."
            )
        self._ontology_ids[ontology_name] = ontology_id
        print(f"   🧩 Resolved ontology {ontology_name}: {ontology_id}")
        return ontology_id

    @staticmethod
    def _json_value(entity: Dict[str, Any]) -> Dict[str, Any]:
        value = entity.get("json_value") or {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _existing_entity(
        self,
        collection_id: str,
        entity_type: str,
        name: str,
        json_value: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if collection_id not in self._collection_entities:
            entities: list[Dict[str, Any]] = []
            seen_ids: set[str] = set()
            seen_pages: set[tuple[str, ...]] = set()
            skip = 0
            while True:
                response = requests.get(
                    self.entity_endpoint,
                    params={
                        "odata_query": f"collection_id eq '{collection_id}'",
                        "skip": skip,
                        "limit": 500,
                    },
                    headers=self.headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                data = response.json()
                if isinstance(data, list):
                    page = data
                elif isinstance(data, dict):
                    page = data.get("items") or data.get("data") or []
                else:
                    raise ValueError("entity API returned an invalid list envelope")
                if not isinstance(page, list):
                    raise ValueError("entity API returned an invalid list envelope")
                signature = tuple(str(item.get("id") or "") for item in page if isinstance(item, dict))
                if signature in seen_pages:
                    raise ValueError("entity pagination repeated a page")
                seen_pages.add(signature)
                for item in page:
                    if not isinstance(item, dict):
                        continue
                    item_id = str(item.get("id") or "")
                    if item_id and item_id in seen_ids:
                        continue
                    if item_id:
                        seen_ids.add(item_id)
                    entities.append(item)
                if len(page) < 500:
                    break
                skip += len(page)
            self._collection_entities[collection_id] = entities
        matches = [
            item for item in self._collection_entities[collection_id]
            if item.get("entity_type") == entity_type and item.get("name") == name
        ]
        if len(matches) > 1:
            raise RuntimeError(
                f"Multiple {entity_type} entities named {name!r} exist in collection "
                f"{collection_id}; refusing an ambiguous update"
            )
        if matches:
            return matches[0]

        identity_fields = self.IDENTITY_FIELDS.get(entity_type)
        if not identity_fields:
            return None
        expected_identity = {
            field: json_value.get(field)
            for field in identity_fields
            if json_value.get(field) not in (None, "")
        }
        if not expected_identity:
            return None
        semantic_matches = []
        for item in self._collection_entities[collection_id]:
            if item.get("entity_type") != entity_type:
                continue
            existing_value = self._json_value(item)
            if all(existing_value.get(field) == value for field, value in expected_identity.items()):
                semantic_matches.append(item)
        if len(semantic_matches) > 1:
            ids = ", ".join(str(item.get("id")) for item in semantic_matches)
            raise RuntimeError(
                f"Multiple legacy {entity_type} entities match the same logical identity: {ids}"
            )
        return semantic_matches[0] if semantic_matches else None

    def create_entity(
        self,
        payload: Dict[str, Any],
        entity_label: str = "Entity",
    ) -> Dict[str, Any]:
        """Create an entity in the Knowledge Hub.

        Args:
            payload: The entity payload (entity_type, ontology_id, name, collection_id, json_value, description).
            entity_label: Label for logging purposes.

        Returns:
            The API response data. API failures raise RuntimeError.
        """
        entity_name = payload.get("name", "Unknown")
        entity_type = payload.get("entity_type", "Unknown")

        # Ontology UUIDs are different in every JazzX instance. Resolve the
        # stable deployed ontology name against the selected gateway instead
        # of using the POC-only IDs embedded in the payload builders.
        ontology_name = self.ontology_name_by_entity_type.get(
            entity_type, self.default_ontology_name
        )
        payload = {
            **payload,
            "ontology_id": self._resolve_ontology_id(ontology_name),
        }

        try:
            collection_id = payload.get("collection_id")
            existing = self._existing_entity(
                collection_id,
                entity_type,
                entity_name,
                payload.get("json_value") or {},
            )
            if existing:
                print(f"🔄 Updating {entity_type} entity: {entity_name}...")
                response = requests.patch(
                    f"{self.entity_endpoint}/{existing['id']}",
                    json={"json_value": payload.get("json_value", {})},
                    headers=self.headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                data = response.json() if response.content else {}
                if not isinstance(data, dict):
                    raise ValueError("entity update API returned a non-object response")
                data.setdefault("id", existing["id"])
                print(f"   ✅ {entity_type} entity updated: {entity_name}")
            else:
                print(f"📋 Creating {entity_type} entity: {entity_name}...")
                response = requests.post(
                    self.entity_endpoint,
                    json=payload,
                    headers=self.headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not data.get("id"):
                    raise ValueError("entity creation API returned no entity id")
                self._collection_entities.setdefault(collection_id, []).append(data)
                print(f"   ✅ {entity_type} entity created: {entity_name}")
            return data
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"❌ Error saving {entity_type} entity ({entity_name}): {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"   Response: {e.response.text}")
            raise RuntimeError(
                f"Could not save {entity_type} entity {entity_name!r}; "
                "stopping to avoid an incomplete loan"
            ) from e


# =============================================================================
# Knowledge Hub Entity Creation Orchestrator
# =============================================================================

# =============================================================================
# Pipeline Updater - Adds loan to the pipeline
# =============================================================================


class PipelineUpdater:
    """Updates the mock pipeline to include a new loan."""

    def __init__(
        self,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        token: str = DEFAULT_TOKEN,
        initialize_missing: bool = False,
        mock_data_path: str = DEFAULT_MOCK_DATA_PATH,
        pipeline_path: str = DEFAULT_PIPELINE_PATH,
    ):
        self.gateway_url = normalize_gateway_url(gateway_url)
        self.initialize_missing = initialize_missing
        self.pipeline_path = "/" + pipeline_path.strip("/")
        self.pipeline_endpoint = f"{self.gateway_url}/mock-server{self.pipeline_path}"
        self.mock_data_endpoint = f"{self.gateway_url}/{mock_data_path.strip('/')}"
        self.pipeline_mock_exists = False
        self.pipeline_mock_id: Optional[int] = None
        self.pipeline_mock_priority = 5
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _empty_pipeline_response() -> Dict[str, Any]:
        """Return the response envelope expected by loanPipelineQuery."""
        return {
            "success": True,
            "status": "COMPLETED",
            "outputVariables": {
                "loan_pipeline_data": {
                    "items": [],
                    "total": 0,
                    "header": {
                        "needs_attention": 0,
                        "total_active_loans": 0,
                        "in_jazz_track": 0,
                        "submitted_to_uw": 0,
                    },
                    "columnMappings": {
                        "Loan.BorrowerName": "Borrower",
                        "Loan.LoanNumber": "Loan Number",
                        "Loan.CurrentMilestoneName": "Current Milestone",
                        "Loan.LoanAmount": "Loan Amount",
                        "Loan.LastModified": "Last Modified",
                        "Fields.1172": "Loan Type",
                        "Loan.DateFileOpened": "Date File Opened",
                        "Loan.DateOfEstimatedCompletion": "Estimated Completion",
                    },
                    "filters": [],
                }
            },
        }

    def fetch_current_pipeline(self) -> Optional[Dict[str, Any]]:
        """Fetch the pipeline response through the mock-data admin API."""
        try:
            print(f"📊 Fetching the GET {self.pipeline_path} mock definition...")
            response = requests.get(
                self.mock_data_endpoint,
                params={
                    "endpoint_path": self.pipeline_path,
                    "method": "GET",
                    "is_active": 1,
                },
                headers=self.headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body = response.json()
            if isinstance(body, list):
                mocks = body
            elif isinstance(body, dict):
                mocks = body.get("items") or body.get("data") or []
            else:
                raise ValueError("mock-data API returned an invalid response envelope")
            if not isinstance(mocks, list) or any(not isinstance(item, dict) for item in mocks):
                raise ValueError("mock-data API returned a non-list response")
            if mocks:
                highest_priority = max(int(item.get("priority", 0)) for item in mocks)
                highest = [
                    item
                    for item in mocks
                    if int(item.get("priority", 0)) == highest_priority
                ]
                selected = max(highest, key=lambda item: int(item.get("id", 0)))
                if len(highest) == 1:
                    self.pipeline_mock_exists = True
                    self.pipeline_mock_id = int(selected["id"])
                    self.pipeline_mock_priority = highest_priority
                    print(
                        f"   ✅ Found active GET {self.pipeline_path} mock "
                        f"(ID: {self.pipeline_mock_id}, priority: {highest_priority})"
                    )
                else:
                    # Older versions created a new priority-5 row every run.
                    # Create one unambiguous winner, then patch it on later runs.
                    self.pipeline_mock_priority = highest_priority + 1
                    print(
                        f"   ⚠️  Found {len(highest)} top-priority pipeline mocks; "
                        f"a new priority-{self.pipeline_mock_priority} mock will be created"
                    )
                response_body = selected.get("response_body") or self._empty_pipeline_response()
                if isinstance(response_body, str):
                    response_body = json.loads(response_body)
                if not isinstance(response_body, dict):
                    raise ValueError("pipeline mock response_body must be a JSON object")
                return response_body
            if self.initialize_missing:
                print(
                    f"   🆕 GET {self.pipeline_path} is not registered; "
                    "preparing a new mock"
                )
                return self._empty_pipeline_response()
            print(
                f"❌ No active GET {self.pipeline_path} mock exists. "
                "Run again with --initialize-pipeline-mock to create it."
            )
            return None
        except (ValueError, TypeError) as e:
            print(f"❌ Invalid response from mock-data API: {e}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching GET {self.pipeline_path} mock definition: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"   Response: {e.response.text}")
            return None

    def _format_borrower_name(self, input_data: Dict[str, Any]) -> str:
        """Format borrower name as 'LastName, FirstName MiddleName'."""
        borrowers = input_data.get("borrowers", [])
        if not borrowers:
            return "Unknown, Borrower"

        primary = borrowers[0]
        first_name = primary.get("firstName", "")
        middle_name = primary.get("middleName", "")
        last_name = primary.get("lastName", "")

        if middle_name:
            return f"{last_name}, {first_name} {middle_name}".strip()
        return f"{last_name}, {first_name}".strip()

    def _get_current_datetime_formatted(self) -> str:
        """Get current datetime in the format 'MM/DD/YYYY H:MM:SS AM/PM'."""
        return self._format_datetime(datetime.now())

    def _get_current_date_formatted(self) -> str:
        """Get current date in the format 'MM/DD/YYYY H:MM:SS AM/PM'."""
        return self._format_datetime(datetime.now())

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        """Format without the platform-specific %-I directive."""
        rendered = value.strftime("%m/%d/%Y %I:%M:%S %p")
        date_part, time_part = rendered.split(" ", 1)
        return f"{date_part} {time_part.lstrip('0')}"

    def _create_loan_pipeline_item(
        self,
        loan_id: str,
        input_data: Dict[str, Any],
        pipeline_stage: str = "Submitted",
        loan_folder: str = "My Pipeline",
        pipeline_loan_number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new pipeline item for the loan."""
        loan_data = input_data.get("loan") or {}
        borrower_name = self._format_borrower_name(input_data)

        # Extract loan details
        loan_number = pipeline_loan_number or loan_data.get("loanNumber", "")
        loan_amount = loan_data.get("loanAmount", 0.0)
        loan_purpose = loan_data.get("loanPurpose", "Purchase")
        loan_product = loan_data.get("loanProduct") or {}
        mortgage_type = loan_product.get("mortgageType", "Conventional")

        # Format loan type (e.g., "Conventional Purchase")
        loan_type = f"{mortgage_type} {loan_purpose}"

        # Dates
        current_datetime = self._get_current_datetime_formatted()
        created_at = loan_data.get("createdAt", "")
        closing_date = loan_data.get("closingDate", "")

        # Format created_at date
        date_file_opened = current_datetime
        if created_at:
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                date_file_opened = self._format_datetime(dt)
            except (ValueError, AttributeError):
                pass

        # Format closing date
        estimated_completion = current_datetime
        if closing_date:
            try:
                dt = datetime.strptime(closing_date, "%Y-%m-%d")
                estimated_completion = self._format_datetime(dt)
            except (ValueError, AttributeError):
                pass

        return {
            "loanId": loan_id,
            "fields": {
                "Loan.BorrowerName": borrower_name,
                "Loan.LoanNumber": loan_number,
                # These are broker-portal fields.  They are intentionally
                # separate from LoanCore.loan_status: the loan list shown by
                # the portal filters on Submitted and a LoanFolder.
                "Loan.CurrentMilestoneName": pipeline_stage,
                "Loan.LoanFolder": loan_folder,
                "Loan.LoanAmount": str(loan_amount),
                "Loan.LastModified": current_datetime,
                "Fields.1172": loan_type,
                "Loan.LoanType": loan_type,
                "Loan.DateFileOpened": date_file_opened,
                "Loan.DateOfEstimatedCompletion": estimated_completion,
            },
        }

    def _get_max_priority(self, pipeline_data: Dict[str, Any]) -> int:
        """Extract the max priority from existing mock data (default 22 based on example)."""
        # Default priority from the example is 22, we'll increment
        return 4

    def add_loan_to_pipeline(
        self,
        loan_id: str,
        input_data: Dict[str, Any],
        pipeline_stage: str = "Submitted",
        loan_folder: str = "My Pipeline",
        pipeline_loan_number: Optional[str] = None,
    ) -> bool:
        """Add a loan to the pipeline.

        Args:
            loan_id: The loan/project ID.
            input_data: The full input JSON data.

        Returns:
            True if successful, False otherwise.
        """
        # 1. Fetch current pipeline
        pipeline_response = self.fetch_current_pipeline()
        if not pipeline_response:
            print("❌ Could not fetch current pipeline, skipping pipeline update")
            return False

        # 2. Create new loan pipeline item
        new_item = self._create_loan_pipeline_item(
            loan_id,
            input_data,
            pipeline_stage=pipeline_stage,
            loan_folder=loan_folder,
            pipeline_loan_number=pipeline_loan_number,
        )
        borrower_name = new_item["fields"]["Loan.BorrowerName"]

        print(f"📝 Adding loan to pipeline: {borrower_name} ({loan_id})")

        # 3. Update pipeline data
        output_vars = pipeline_response.setdefault("outputVariables", {})
        if not isinstance(output_vars, dict):
            raise RuntimeError("pipeline outputVariables must be an object")
        loan_pipeline_data = output_vars.setdefault("loan_pipeline_data", {})
        if not isinstance(loan_pipeline_data, dict):
            raise RuntimeError("pipeline loan_pipeline_data must be an object")

        items = loan_pipeline_data.get("items", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise RuntimeError("pipeline items must be a list of objects")
        loan_number = new_item["fields"].get("Loan.LoanNumber")
        existing_item = None
        for item in items:
            fields = item.get("fields")
            item_loan_number = fields.get("Loan.LoanNumber") if isinstance(fields, dict) else None
            if str(item.get("loanId")) == str(loan_id) or str(item_loan_number) == str(loan_number):
                existing_item = item
                break

        if existing_item is None:
            # Project is not present in the broker portal response; add it.
            items.append(new_item)
            print("   ➕ Loan was not listed; adding it to the broker portal response")
        else:
            # Repair entries created by older versions of this script.  They
            # commonly have Processing and no LoanFolder, so the portal's
            # Submitted/My Pipeline filters hide them.
            existing_item["loanId"] = loan_id
            existing_fields = existing_item.setdefault("fields", {})
            if not isinstance(existing_fields, dict):
                existing_fields = {}
                existing_item["fields"] = existing_fields
            changed = False
            for key, value in new_item["fields"].items():
                if existing_fields.get(key) != value:
                    existing_fields[key] = value
                    changed = True
            if not changed:
                print("   ✅ Loan is already correctly listed in broker portal")
                return True
            print("   🔄 Loan exists; repairing its broker portal fields")

        # Update total count
        new_total = len(items)
        loan_pipeline_data["items"] = items
        loan_pipeline_data["total"] = new_total

        # Update header
        header = loan_pipeline_data.get("header") or {}
        if not isinstance(header, dict):
            header = {}
        header["total_active_loans"] = new_total
        loan_pipeline_data["header"] = header

        # Update filter values used by the broker portal loan list.
        filters = loan_pipeline_data.get("filters", [])
        if not isinstance(filters, list):
            filters = []
        filter_values = {
            "Loan.BorrowerName": borrower_name,
            "Loan.CurrentMilestoneName": pipeline_stage,
            "Loan.LoanFolder": loan_folder,
        }
        existing_filter_columns = set()
        for filter_item in filters:
            if not isinstance(filter_item, dict):
                continue
            column = filter_item.get("column")
            if column not in filter_values:
                continue
            existing_filter_columns.add(column)
            values = filter_item.get("values", [])
            if not isinstance(values, list):
                values = []
            if filter_values[column] not in values:
                values.append(filter_values[column])
            filter_item["values"] = values
        for column, value in filter_values.items():
            if column not in existing_filter_columns:
                filters.append({"column": column, "values": [value]})
        loan_pipeline_data["filters"] = filters

        # Update the response
        output_vars["loan_pipeline_data"] = loan_pipeline_data

        # 4. Create or update the GET /pipeline mock definition.
        new_priority = self.pipeline_mock_priority

        mock_payload = {
            "description": "Mock response for pipeline views with loan details",
            "endpoint_path": self.pipeline_path,
            "method": "GET",
            "is_active": 1,
            "priority": new_priority,
            "response_status": 200,
            "response_body": pipeline_response,
        }

        # 5. PATCH an existing mock, or POST a new definition.
        try:
            if self.pipeline_mock_exists:
                print(f"📤 Patching the existing GET {self.pipeline_path} mock...")
                response = requests.patch(
                    f"{self.mock_data_endpoint}/{self.pipeline_mock_id}",
                    json={"response_body": pipeline_response},
                    headers=self.headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            else:
                print(
                    f"📤 Creating GET {self.pipeline_path} mock "
                    f"(priority: {new_priority})..."
                )
                response = requests.post(
                    self.mock_data_endpoint,
                    json=mock_payload,
                    headers=self.headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            response.raise_for_status()
            print(f"   ✅ GET {self.pipeline_path} mock saved successfully")
            print(f"   📊 Total loans in pipeline: {new_total}")
            return True
        except requests.exceptions.RequestException as e:
            print(f"❌ Error updating pipeline: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"   Response: {e.response.text}")
            return False


class KnowledgeHubEntityCreator:
    """Orchestrates creation of all Knowledge Hub entities for a loan."""

    def __init__(
        self,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        token: str = DEFAULT_TOKEN,
        ontology_name: str = DEFAULT_ONTOLOGY_NAME,
        loan_project_ontology_name: str = DEFAULT_LOAN_PROJECT_ONTOLOGY_NAME,
        loan_details_ontology_name: str = DEFAULT_LOAN_DETAILS_ONTOLOGY_NAME,
    ):
        self.entity_api = EntityAPI(
            gateway_url=gateway_url,
            token=token,
            ontology_name=ontology_name,
            loan_project_ontology_name=loan_project_ontology_name,
            loan_details_ontology_name=loan_details_ontology_name,
        )

    def create_all_entities(
        self,
        loan_id: str,
        collection_id: str,
        input_data: Dict[str, Any],
        description: str = "Auto-generated loan",
    ) -> Dict[str, Any]:
        """Create all Knowledge Hub entities for a loan.

        Args:
            loan_id: The external loan ID.
            collection_id: The collection ID from project creation.
            input_data: The full input JSON data.
            description: Description for the entities.

        Returns:
            A dict with results for each entity type.
        """
        results = {}

        # 1. Create LoanProject entity
        lp_payload = LoanProjectPayloadBuilder.build(
            loan_id=loan_id,
            collection_id=collection_id,
            project_id=loan_id,
            loan_number=(input_data.get("loan") or {}).get("loanNumber", ""),
            description=description,
        )
        results["loan_project"] = self.entity_api.create_entity(lp_payload)

        # 2. Create LoanCore entity
        lc_payload = LoanCorePayloadBuilder.build(
            loan_id=loan_id,
            collection_id=collection_id,
            input_data=input_data,
            description=description,
        )
        results["loan_core"] = self.entity_api.create_entity(lc_payload)

        # 3. Create Borrower entities
        borrower_payloads = BorrowerPayloadBuilder.build_all(
            collection_id=collection_id,
            input_data=input_data,
            description=description,
        )
        borrower_results = []
        borrower_mappings = []
        for payload, external_id in borrower_payloads:
            result = self.entity_api.create_entity(payload)
            if result:
                result["_external_id"] = external_id
            borrower_results.append(result)
            if result:
                borrower_mappings.append(
                    {
                        "internal_id": result.get("id"),
                        "external_id": external_id,
                    }
                )
            else:
                borrower_mappings.append(None)
        results["borrowers"] = borrower_results

        # 4. Create Asset entities
        asset_payloads = AssetPayloadBuilder.build_all(
            collection_id=collection_id,
            input_data=input_data,
            borrower_mappings=borrower_mappings,
            description=description,
        )
        asset_results = []
        for payload, external_id, borrower_id in asset_payloads:
            result = self.entity_api.create_entity(payload)
            if result:
                result["_external_id"] = external_id
                result["_borrower_id"] = borrower_id
            asset_results.append(result)
        results["assets"] = asset_results

        # 5. Create Income entities
        income_payloads = IncomePayloadBuilder.build_all(
            collection_id=collection_id,
            input_data=input_data,
            borrower_mappings=borrower_mappings,
            description=description,
        )
        income_results = []
        income_external_ids = []
        for payload, external_id, borrower_id in income_payloads:
            result = self.entity_api.create_entity(payload)
            if result:
                result["_external_id"] = external_id
                result["_borrower_id"] = borrower_id
            income_results.append(result)
            income_external_ids.append(external_id)
        results["incomes"] = income_results

        # 6. Create Liability entities
        liability_payloads = LiabilityPayloadBuilder.build_all(
            collection_id=collection_id,
            input_data=input_data,
            borrower_mappings=borrower_mappings,
            description=description,
        )
        liability_results = []
        for payload, external_id, borrower_id in liability_payloads:
            result = self.entity_api.create_entity(payload)
            if result:
                result["_external_id"] = external_id
                result["_borrower_id"] = borrower_id
            liability_results.append(result)
        results["liabilities"] = liability_results

        # 7. Create SubjectProperty entity
        sp_result = SubjectPropertyPayloadBuilder.build(
            collection_id=collection_id,
            input_data=input_data,
            description=description,
        )
        if sp_result:
            payload, external_id = sp_result
            result = self.entity_api.create_entity(payload)
            if result:
                result["_external_id"] = external_id
            results["subject_property"] = result
        else:
            print(
                "⚠️  No subject_property data found, skipping SubjectProperty entity creation"
            )
            results["subject_property"] = None

        # 8. Create Employment entities (linked to income external IDs)
        employment_payloads = EmploymentPayloadBuilder.build_all(
            collection_id=collection_id,
            input_data=input_data,
            borrower_mappings=borrower_mappings,
            income_external_ids=income_external_ids,
            description=description,
        )
        employment_results = []
        for payload, external_id, borrower_id in employment_payloads:
            result = self.entity_api.create_entity(payload)
            if result:
                result["_external_id"] = external_id
                result["_borrower_id"] = borrower_id
            employment_results.append(result)
        results["employments"] = employment_results

        # 9. Create CreditReport entities (one per borrower with credit data)
        credit_payloads = CreditReportPayloadBuilder.build_all(
            collection_id=collection_id,
            input_data=input_data,
            borrower_mappings=borrower_mappings,
            description=description,
        )
        credit_results = []
        for payload, external_id, borrower_id in credit_payloads:
            result = self.entity_api.create_entity(payload)
            if result:
                result["_external_id"] = external_id
                result["_borrower_id"] = borrower_id
            credit_results.append(result)
        results["credit_reports"] = credit_results

        # 10. Create LoanApplication entity (hub linking all entities)
        borrower_refs = [
            {"external_id": br.get("_external_id"), "internal_id": br.get("id")}
            for br in borrower_results
            if br
        ]
        asset_refs = [
            {
                "borrower_id": ar.get("_borrower_id"),
                "external_id": ar.get("_external_id"),
                "internal_id": ar.get("id"),
            }
            for ar in asset_results
            if ar
        ]
        income_refs = [
            {
                "borrower_id": ir.get("_borrower_id"),
                "external_id": ir.get("_external_id"),
                "internal_id": ir.get("id"),
            }
            for ir in income_results
            if ir
        ]
        liability_refs = [
            {
                "borrower_id": lr.get("_borrower_id"),
                "external_id": lr.get("_external_id"),
                "internal_id": lr.get("id"),
            }
            for lr in liability_results
            if lr
        ]
        employment_refs = [
            {
                "borrower_id": er.get("_borrower_id"),
                "external_id": er.get("_external_id"),
                "internal_id": er.get("id"),
            }
            for er in employment_results
            if er
        ]
        credit_refs = [
            {
                "borrower_id": cr.get("_borrower_id"),
                "external_id": cr.get("_external_id"),
                "internal_id": cr.get("id"),
            }
            for cr in credit_results
            if cr
        ]

        la_payload = LoanApplicationPayloadBuilder.build(
            loan_id=loan_id,
            collection_id=collection_id,
            loan_core_internal_id=(
                results["loan_core"].get("id") if results["loan_core"] else None
            ),
            borrower_refs=borrower_refs,
            asset_refs=asset_refs,
            income_refs=income_refs,
            liability_refs=liability_refs,
            employment_refs=employment_refs,
            credit_report_refs=credit_refs,
            description=description,
        )
        results["loan_application"] = self.entity_api.create_entity(la_payload)

        # 11. Create LoanDetails entity
        ld_payload = LoanDetailsPayloadBuilder.build(
            loan_id=loan_id,
            collection_id=collection_id,
            input_data=input_data,
            description=description,
        )
        results["loan_details"] = self.entity_api.create_entity(ld_payload)

        return results

    def print_summary(self, results: Dict[str, Any], loan_id: str, collection_id: str):
        """Print a summary of created entities."""
        print(f"\n{'=' * 70}")
        print("✅ KNOWLEDGE HUB ENTITIES CREATED")
        print(f"{'=' * 70}")
        print(f"   Loan ID:           {loan_id}")
        print(f"   Collection ID:     {collection_id}")
        print(f"   LoanProject:       {'✅' if results.get('loan_project') else '❌'}")
        print(f"   LoanCore:          {'✅' if results.get('loan_core') else '❌'}")

        borrowers = results.get("borrowers", [])
        print(
            f"   Borrowers:         {sum(1 for b in borrowers if b)}/{len(borrowers)}"
        )

        assets = results.get("assets", [])
        print(f"   Assets:            {sum(1 for a in assets if a)}/{len(assets)}")

        incomes = results.get("incomes", [])
        print(f"   Income Sources:    {sum(1 for i in incomes if i)}/{len(incomes)}")

        liabilities = results.get("liabilities", [])
        print(
            f"   Liabilities:       {sum(1 for l in liabilities if l)}/{len(liabilities)}"
        )

        print(
            f"   SubjectProperty:   {'✅' if results.get('subject_property') else '❌'}"
        )

        employments = results.get("employments", [])
        print(
            f"   Employment:        {sum(1 for e in employments if e)}/{len(employments)}"
        )

        credit_reports = results.get("credit_reports", [])
        print(
            f"   CreditReports:     {sum(1 for c in credit_reports if c)}/{len(credit_reports)}"
        )

        print(
            f"   LoanApplication:   {'✅' if results.get('loan_application') else '❌'}"
        )
        print(f"   LoanDetails:       {'✅' if results.get('loan_details') else '❌'}")
        print()


# =============================================================================
# Main Function
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Create or update a JazzX loan project, Knowledge Hub entities, "
            "and its mock pipeline row"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create/update a loan using the configured gateway
  python add_loan_mock_data.py --input loan_data.json --project-name 12345_1

  # Override gateway URL or token
  python add_loan_mock_data.py --input loan_data.json --gateway-url https://demo-gw.jazzx.co --token <token>

  # Validate locally without API calls
  python add_loan_mock_data.py --input loan_data.json --validate-only

Input JSON format:
  See sample_loan_input.json for the expected structure.
        """,
    )

    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to the input JSON file with loan data",
    )

    parser.add_argument(
        "--gateway-url",
        default=DEFAULT_GATEWAY_URL,
        help="JazzX gateway URL (or set JAZZX_GATEWAY_URL)",
    )

    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token for authentication (or set JAZZX_TOKEN)",
    )

    parser.add_argument(
        "--loan-id",
        default=None,
        help="Existing Assistant project UUID; skips project lookup/creation",
    )

    parser.add_argument(
        "--collection-id",
        default=None,
        help="Existing Knowledge Hub collection ID when the project API does not return it",
    )

    parser.add_argument(
        "--project-name",
        default=None,
        help=(
            "Override the Assistant project name and the loan number displayed "
            "in the mock pipeline without changing the input JSON"
        ),
    )

    parser.add_argument(
        "--ontology-name",
        default=DEFAULT_ONTOLOGY_NAME,
        help="Ontology name for standard mortgage entities",
    )

    parser.add_argument(
        "--loan-project-ontology-name",
        default=DEFAULT_LOAN_PROJECT_ONTOLOGY_NAME,
        help="Ontology name for LoanProject entities",
    )

    parser.add_argument(
        "--loan-details-ontology-name",
        default=DEFAULT_LOAN_DETAILS_ONTOLOGY_NAME,
        help="Ontology name for LoanDetails entities",
    )

    parser.add_argument(
        "--pipeline-stage",
        default="Submitted",
        help="Broker-portal milestone for the loan list (default: Submitted)",
    )

    parser.add_argument(
        "--loan-folder",
        default="My Pipeline",
        help="Broker-portal loan folder (default: My Pipeline)",
    )

    parser.add_argument(
        "--mock-data-path",
        default=DEFAULT_MOCK_DATA_PATH,
        help="Gateway path for the mock-data administration API",
    )

    parser.add_argument(
        "--pipeline-path",
        default=DEFAULT_PIPELINE_PATH,
        help="Endpoint path registered for the pipeline GET mock",
    )

    parser.add_argument(
        "--skip-pipeline-update",
        action="store_true",
        help="Create/update the project and entities without changing pipeline mock data",
    )

    parser.add_argument(
        "--initialize-pipeline-mock",
        action="store_true",
        help=(
            "If the mock-data admin API has no active GET /pipeline definition, "
            "create one and add this loan. Use only when the target environment's "
            "loanPipelineQuery is configured to read /pipeline from Mock Server."
        ),
    )

    parser.add_argument(
        "--pipeline-only",
        action="store_true",
        help=(
            "Reuse the named project and only add/repair its pipeline row; do not "
            "create or update any Knowledge Hub entities."
        ),
    )

    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate and normalize the input locally without making API calls",
    )

    args = parser.parse_args()
    if args.skip_pipeline_update and args.initialize_pipeline_mock:
        parser.error(
            "--skip-pipeline-update and --initialize-pipeline-mock cannot be used together"
        )

    # -------------------------------------------------------------------------
    # Validate input file
    # -------------------------------------------------------------------------
    if not args.input.is_file():
        print(f"❌ Input file not found: {args.input}")
        return 1

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            input_data = json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        print(f"❌ Could not read valid JSON from input file: {e}")
        return 1
    if not isinstance(input_data, dict):
        print("❌ Input JSON root must be an object")
        return 1

    # Accept the extracted snake_case schema without modifying the source file.
    input_data = normalize_extracted_loan(input_data)
    if args.project_name:
        input_data["project_name"] = args.project_name.strip()
    if not input_data.get("project_name"):
        input_data["project_name"] = str(
            (input_data.get("loan") or {}).get("loanNumber") or ""
        ).strip()
    if not input_data.get("project_description"):
        input_data["project_description"] = "Auto-generated loan"

    validation = validate_input(input_data)
    if validation.has_errors:
        print_validation_results(validation)
        print("❌ Input validation failed; no API calls were made")
        return 1
    if args.validate_only:
        print_validation_results(validation, verbose=True)
        print("✅ Validation-only mode completed; no API calls were made")
        return 0

    # -------------------------------------------------------------------------
    # Resolve token
    # -------------------------------------------------------------------------
    try:
        token = normalize_token(args.token or os.environ.get("JAZZX_TOKEN") or DEFAULT_TOKEN)
        gateway_url = normalize_gateway_url(args.gateway_url)
        loan_id = validated_uuid(args.loan_id, "--loan-id")
        collection_id = validated_uuid(args.collection_id, "--collection-id")
    except ValueError as error:
        parser.error(str(error))

    # -------------------------------------------------------------------------
    # Determine loan_id and mock_url
    # -------------------------------------------------------------------------
    project_details = input_data.get("project_details") or {}
    if not isinstance(project_details, dict):
        parser.error("project_details must be an object")
    project_name = args.project_name or input_data.get("project_name") or (
        project_details.get("project_number")
    ) or f"mock_loan_{uuid.uuid4().hex[:8]}"
    project_name = str(project_name).strip()
    if not project_name or len(project_name) > 255 or any(character in project_name for character in "\r\n"):
        parser.error("project name cannot be blank, exceed 255 characters, or contain newlines")
    project_desc = str(input_data.get("project_description") or "Auto-generated mock loan")

    if loan_id:
        if not collection_id and not args.pipeline_only:
            parser.error("--collection-id is required with --loan-id unless --pipeline-only is used")
        project = {"id": loan_id, "collection_id": collection_id, "name": project_name}
        print(f"♻️  Using existing project: {loan_id}")
    else:
        creator = ProjectCreator(gateway_url=gateway_url, token=token)
        project = creator.get_or_create_project(
            name=project_name,
            description=project_desc,
            fallback_collection_id=collection_id,
            require_collection_id=not args.pipeline_only,
            create_if_absent=not args.pipeline_only,
        )

    loan_id = validated_uuid(str(project.get("id") or ""), "project id")
    collection_id = project.get("collection_id")
    if collection_id:
        collection_id = validated_uuid(str(collection_id), "project collection_id")

    if args.pipeline_only:
        print("⏭️  Pipeline-only mode: leaving Knowledge Hub entities unchanged")
    else:
        kh_creator = KnowledgeHubEntityCreator(
            gateway_url=gateway_url,
            token=token,
            ontology_name=args.ontology_name,
            loan_project_ontology_name=args.loan_project_ontology_name,
            loan_details_ontology_name=args.loan_details_ontology_name,
        )
        results = kh_creator.create_all_entities(
            loan_id=loan_id,
            collection_id=collection_id,
            input_data=input_data,
            description=project_desc,
        )
        kh_creator.print_summary(results, loan_id, collection_id)

    if args.skip_pipeline_update:
        print("⏭️  Skipping mock pipeline update")
        return 0
    pipeline_updater = PipelineUpdater(
        gateway_url=gateway_url,
        token=token,
        initialize_missing=args.initialize_pipeline_mock,
        mock_data_path=args.mock_data_path,
        pipeline_path=args.pipeline_path,
    )
    updated = pipeline_updater.add_loan_to_pipeline(
        loan_id=loan_id,
        input_data=input_data,
        pipeline_stage=args.pipeline_stage,
        loan_folder=args.loan_folder,
        pipeline_loan_number=project_name,
    )
    return 0 if updated else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, PydanticValidationError) as error:
        print(f"❌ {error}", file=sys.stderr)
        raise SystemExit(1) from error
