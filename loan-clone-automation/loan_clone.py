#!/usr/bin/env python3
"""Clone Knowledge Hub entities for a JazzX loan into a new project.

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

  # Clone the loan without MACER findings and conditions
  JAZZX_TOKEN=... python loan_clone.py --reference-loan-number 1441010 \
    --target-loan-number 1441010_1 --source-collection-id <loan-collection-uuid> \
    --remove-macer
"""

from __future__ import annotations

import argparse
import http.client
import io
import itertools
import json
import math
import os
import re
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_GATEWAY_URL = os.getenv("JAZZX_GATEWAY_URL", "")
# The gateway intermittently drops or truncates responses under this workload.
# Repeating an idempotent request is safe and costs only the refetch. POST is
# excluded: an upload or entity create that failed late may already have landed.
REQUEST_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 2
IDEMPOTENT_METHODS = {"GET", "HEAD", "PATCH"}
# Reserved UUID range for not-yet-created references. Valid per RFC 4122 (version
# nibble 4, variant 8) so ontology id validation accepts it, and constant enough to
# recognise a placeholder that survived the patch pass.
PLACEHOLDER_UUID_PREFIX = "00000000-0000-4000-8000-"
# The earlier format was rejected by ontology id validation, but entities created
# before that fix can still hold one. Detected so a resumed run cannot leave a
# stranded reference behind silently.
LEGACY_PLACEHOLDER_PREFIX = "pending-reference-"
PLACEHOLDER_PREFIXES = (PLACEHOLDER_UUID_PREFIX, LEGACY_PLACEHOLDER_PREFIX)
# urlopen's timeout arms per socket operation, so a server that trickles bytes can
# hold a request open forever. The socket gets this shorter stall budget while
# --document-timeout bounds the document as a whole, enforced during the read.
DOCUMENT_STALL_SECONDS = 120
READ_CHUNK_BYTES = 1 << 20
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

# MACER workflow entities.  Document-related entities are intentionally not
# included here and are retained when --remove-macer is supplied.
MACER_ENTITY_TYPES = frozenset({
    "MacerInvocationEvent",
    "Finding",
    "FindingSet",
    "LoanFindingsSummary",
    "Condition",
    "MergedConditionSet",
})


def validate_uuid(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID, got {value!r}") from exc


def nested_collection_id(value: Any) -> str | None:
    """Find a collection_id in common nested project response envelopes."""
    if isinstance(value, dict):
        for key in ("collection_id", "collectionId", "knowledge_hub_collection_id"):
            candidate = value.get(key)
            if candidate:
                try:
                    return str(uuid.UUID(str(candidate)))
                except (ValueError, AttributeError):
                    continue
        for child in value.values():
            found = nested_collection_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = nested_collection_id(child)
            if found:
                return found
    return None


class JazzXClient:
    """Small stdlib-only client for the two JazzX APIs used by this workflow."""

    def __init__(self, gateway_url: str, token: str | None, user_session_id: str | None,
                 user_agent: str = "JazzX-Loan-Clone-Automation/1.0", timeout: float = 60,
                 document_timeout: float | None = None):
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
        if document_timeout is not None and document_timeout <= 0:
            raise ValueError("--document-timeout must be greater than zero")
        self.gateway_url = gateway_url
        self.timeout = timeout
        # Document transfer moves whole files and is legitimately slower than the
        # JSON calls, so it gets its own budget rather than borrowing --timeout.
        self.document_timeout = document_timeout if document_timeout is not None else timeout
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
        # Decoding happens outside the retry: a malformed body is a real answer,
        # not a transient fault, so it must not be retried.
        raw_bytes, _ = self._send(request, self.timeout, f"{method} {path}")
        raw = raw_bytes.decode("utf-8")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{method} {path} returned invalid JSON") from exc

    def _send(self, request: Request, timeout: float, label: str,
              deadline_read: bool = False) -> tuple[bytes, str | None]:
        """Send a request, retrying transient failures on idempotent methods.

        GET and PATCH are safe to repeat -- a read has no effect, and these PATCHes
        set a whole json_value rather than applying a delta, so sending one twice
        lands on the same state. POST is not: an upload or entity create that timed
        out may already have been applied, and repeating it would duplicate.
        """
        method = request.get_method()
        attempts = REQUEST_ATTEMPTS if method in IDEMPOTENT_METHODS else 1
        for attempt in range(1, attempts + 1):
            try:
                with urlopen(request, timeout=timeout) as response:
                    body = (self._read_within_deadline(response, label) if deadline_read
                            else response.read())
                    return body, response.headers.get_content_type()
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                if attempt < attempts and (exc.code >= 500 or exc.code == 429):
                    reason: Any = f"HTTP {exc.code}"
                else:
                    raise RuntimeError(f"{label} failed ({exc.code}): {detail}") from exc
            except (URLError, TimeoutError, http.client.IncompleteRead,
                    http.client.HTTPException, ConnectionError, OSError) as exc:
                if attempt >= attempts:
                    raise RuntimeError(
                        f"{label} failed after {attempt} attempt(s): {getattr(exc, 'reason', exc)}"
                    ) from exc
                reason = getattr(exc, "reason", exc)
            delay = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(f"Warning: {label} failed ({reason}); retrying in {delay:g}s "
                  f"[{attempt}/{attempts - 1}]", file=sys.stderr)
            time.sleep(delay)
        raise RuntimeError(f"{label} failed after {attempts} attempt(s)")

    def _read_within_deadline(self, response: Any, label: str) -> bytes:
        """Read a response body under a wall-clock budget.

        response.read() with no argument blocks until the body is complete, and
        urlopen's timeout only bounds each individual socket operation. Reading in
        chunks lets the elapsed time be checked between them, so one document can
        never consume the run.
        """
        started = time.monotonic()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(READ_CHUNK_BYTES)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            elapsed = time.monotonic() - started
            if elapsed > self.document_timeout:
                raise RuntimeError(
                    f"{label} exceeded the {self.document_timeout:g}s document deadline "
                    f"after {total} bytes; giving up on this document"
                )

    def request_bytes(self, method: str, path_or_url: str, *, payload: bytes | None = None,
                      headers: dict[str, str] | None = None,
                      query: dict[str, Any] | None = None) -> tuple[bytes, str]:
        """Make a request without assuming the response is JSON."""
        url = path_or_url if path_or_url.startswith(("https://", "http://")) else f"{self.gateway_url}{path_or_url}"
        if query:
            url = f"{url}?{urlencode(query)}"
        request_headers = dict(self.headers)
        if headers:
            request_headers.update(headers)
        request = Request(url, data=payload, headers=request_headers, method=method)
        # The socket gets the shorter stall budget; --document-timeout bounds the
        # whole body, enforced between chunks by _read_within_deadline.
        socket_timeout = min(self.document_timeout, DOCUMENT_STALL_SECONDS)
        return self._send(request, socket_timeout, f"{method} {path_or_url}", deadline_read=True)

    def documents_in_collection(self, collection_id: str) -> list[dict[str, Any]]:
        """Fetch document resources without waiting for their processing state."""
        documents: list[dict[str, Any]] = []
        seen: set[str] = set()
        skip = 0
        while True:
            page = self.request(
                "GET", f"/knowledge_hub/api/v1/collections/{collection_id}/documents",
                query={"skip": skip, "limit": 100},
            )
            if isinstance(page, list):
                items = page
            elif isinstance(page, dict):
                items = page.get("items") or page.get("data") or page.get("documents") or page.get("results") or []
            else:
                raise RuntimeError("document API returned an invalid list envelope")
            if not isinstance(items, list) or not items:
                break
            for document in items:
                if not isinstance(document, dict):
                    continue
                document_id = str(document.get("id") or document.get("document_id") or "")
                if document_id and document_id not in seen:
                    seen.add(document_id)
                    documents.append(document)
            if len(items) < 100:
                break
            skip += 100
        return documents

    @staticmethod
    def document_filename(document: dict[str, Any], fallback_id: str) -> str:
        return str(
            document.get("filename") or document.get("file_name") or document.get("name")
            or document.get("original_filename") or f"document_{fallback_id}"
        )

    @staticmethod
    def document_download_url(document: dict[str, Any]) -> str | None:
        keys = ("download_url", "downloadUrl", "file_url", "fileUrl", "content_url", "contentUrl")
        for key in keys:
            value = document.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def download_document(self, document: dict[str, Any], document_id: str,
                          collection_id: str) -> tuple[bytes, str]:
        """Fetch one document's bytes.

        Knowledge Hub exposes no per-document download URL, so the listing carries
        metadata only. The collection download endpoint is the real route, and it
        answers with a zip archive even when a single document id is asked for.
        A URL on the document is still honoured first, for any deployment that
        does serve one.
        """
        url = self.document_download_url(document)
        if url:
            content, content_type = self.request_bytes("GET", url)
            if content_type == "application/json" or content[:1] in (b"{", b"["):
                raise RuntimeError(
                    f"Source document {document_id} download returned metadata instead of file bytes"
                )
            return content, content_type

        archive, content_type = self.request_bytes(
            "GET", f"/knowledge_hub/api/v1/collections/{collection_id}/download",
            query={"document_ids": document_id},
        )
        if content_type == "application/json" or archive[:1] in (b"{", b"["):
            raise RuntimeError(
                f"Source document {document_id} download returned metadata instead of file bytes"
            )
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                names = [name for name in bundle.namelist() if not name.endswith("/")]
                if not names:
                    raise RuntimeError(
                        f"Source document {document_id} download returned an empty archive"
                    )
                if len(names) > 1:
                    print(
                        f"Warning: archive for {document_id} holds {len(names)} files; using {names[0]!r}",
                        file=sys.stderr,
                    )
                return bundle.read(names[0]), "application/octet-stream"
        except zipfile.BadZipFile:
            # Some deployments may hand back the raw file rather than an archive.
            return archive, content_type or "application/octet-stream"

    def upload_document(self, collection_id: str, filename: str, content: bytes,
                        source_document_id: str) -> dict[str, Any]:
        boundary = f"----JazzXLoanClone{uuid.uuid4().hex}"
        metadata = json.dumps({
            "source": "loan-clone-automation",
            "source_document_id": source_document_id,
            "content_type": "application/octet-stream",
        })
        fields = {
            "filename": filename,
            "meta_data": metadata,
            # Both match the Knowledge Hub defaults. build_knowledge_graph="true"
            # makes /newdocuments build the graph inside the request, which routinely
            # runs past any sane socket timeout; --copy-documents deliberately does
            # not wait for processing, so the graph is not built here.
            "storage_only": "false",
            "build_knowledge_graph": "false",
        }
        body = bytearray()
        for name, value in fields.items():
            body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        body.extend(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        body.extend(content)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        raw, content_type = self.request_bytes(
            "POST", f"/knowledge_hub/api/v1/collections/{collection_id}/newdocuments",
            payload=bytes(body), headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "X-Upload-Source": "internal"},
        )
        if not raw:
            return {}
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = {"raw_response_content_type": content_type}
        return response if isinstance(response, dict) else {"response": response}

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

    def get_or_create_project(self, project_name: str, description: str,
                              fallback_collection_id: str | None = None) -> tuple[dict[str, Any], bool]:
        """Reuse the one active project with this name, or create it.

        Reusing an existing project makes an interrupted clone resumable.  More
        than one active match remains an error because the target collection
        would be ambiguous.
        """
        matches = self.active_projects_named(project_name)
        if len(matches) > 1:
            ids = ", ".join(str(item.get("id")) for item in matches)
            raise RuntimeError(f"Multiple active projects named {project_name!r} exist ({ids}); refusing an ambiguous resume")
        if matches:
            project = matches[0]
            # The project list endpoint may omit collection_id. Fetch the
            # project detail before treating the existing project as invalid.
            if not project.get("collection_id") and project.get("id"):
                detail = self.request(
                    "GET", f"/assistant/api/v1/project/{project['id']}", project_api=True
                )
                if isinstance(detail, dict):
                    project = {**project, **detail}
                    if isinstance(detail.get("project"), dict):
                        project = {**project, **detail["project"]}
                    if not project.get("collection_id"):
                        project["collection_id"] = nested_collection_id(detail)
            if not project.get("collection_id") and fallback_collection_id:
                project["collection_id"] = fallback_collection_id
            if not project.get("collection_id"):
                raise RuntimeError(
                    f"Existing project {project.get('id')} has no collection_id in the "
                    "project list or detail response; pass --target-collection-id to resume it"
                )
            project["id"] = validate_uuid(str(project.get("id") or ""), "existing project id")
            project["collection_id"] = validate_uuid(
                str(project.get("collection_id")), "existing project collection_id"
            )
            return project, True
        return self.create_project(project_name, description), False

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


def clone_entity_name(source: dict[str, Any], loan_number: str) -> str:
    """Return a stable name so an interrupted clone can be resumed safely."""
    return f"{loan_number}_{source.get('entity_type', 'Entity')}_{source['id']}"


def clone_payload(source: dict[str, Any], collection_id: str, loan_number: str, project_id: str,
                  id_mapping: dict[str, str], placeholder_ids: dict[str, str]) -> dict[str, Any]:
    payload = {key: deepcopy(value) for key, value in source.items() if key not in READ_ONLY_FIELDS}
    payload["collection_id"] = collection_id
    payload["name"] = clone_entity_name(source, loan_number)
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
                 expected_json: dict[str, dict[str, Any]],
                 allow_validation_warnings: bool = False,
                 excluded_ids: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Read the target back and validate every created entity's persisted JSON.

    Returns (failures, carried_warnings). Structural problems -- a missing entity,
    the wrong collection, JSON that does not match the payload, a surviving source
    ID -- are always failures: they mean the clone is wrong.

    Semantic problems are different. They are data-quality complaints about values
    the SOURCE loan already had (an `employmentType` of 'N/A', say). A faithful
    clone necessarily reproduces them, so failing here would reject a correct copy
    for a fault it did not introduce, and would make --allow-validation-warnings
    self-defeating: it would let the run start and then guarantee it cannot finish.
    With that flag they are carried out as warnings instead.
    """
    actual = client.entities_in_collection(collection_id)
    failures: list[str] = []
    carried_warnings: list[str] = []
    excluded_ids = excluded_ids or set()
    macer_reference_count = 0
    macer_referring_entities: set[str] = set()
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
            labelled = [f"target entity {item['target_id']}: {warning}" for warning in target_warnings]
            if allow_validation_warnings:
                carried_warnings.extend(labelled)
            else:
                failures.extend(labelled)
    for entity in actual:
        values = list(nested_values(entity.get("json_value", {})))
        # A placeholder still present means the patch pass missed this entity, and
        # it now points at an entity that does not exist. Always fatal.
        leftover = sorted({value for value in values if isinstance(value, str)
                           and value.startswith(PLACEHOLDER_PREFIXES)})
        if leftover:
            failures.append(
                f"target entity {entity.get('id')} still holds unresolved placeholders: {leftover}"
            )
        old_ids = {value for value in values if isinstance(value, str) and value in source_ids}
        if not old_ids:
            continue
        # A reference to an entity that --remove-macer deliberately did not clone
        # cannot be remapped: nothing was created to point at. The source id is
        # what remains, and that is the documented consequence of the flag rather
        # than a broken clone. References to anything else are a real failure --
        # that id should have been remapped and was not.
        dangling_macer = old_ids & excluded_ids
        genuine = old_ids - excluded_ids
        if genuine:
            failures.append(
                f"target entity {entity.get('id')} still contains source IDs: {sorted(genuine)}"
            )
        if dangling_macer:
            macer_reference_count += len(dangling_macer)
            macer_referring_entities.add(str(entity.get("id")))
    if macer_referring_entities:
        carried_warnings.append(
            f"{len(macer_referring_entities)} target entities reference "
            f"{macer_reference_count} MACER entities that --remove-macer excluded; "
            f"those references keep their source ids because nothing was cloned to point at"
        )
    return failures, carried_warnings


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


def remove_macer_entities(entities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split source entities into cloneable entities and omitted MACER entities."""
    kept = [entity for entity in entities if entity.get("entity_type") not in MACER_ENTITY_TYPES]
    removed = [entity for entity in entities if entity.get("entity_type") in MACER_ENTITY_TYPES]
    return kept, removed


def summarise_lines(lines: list[str], limit: int = 10) -> list[str]:
    """First `limit` lines, then a count of what was left out.

    Verification can produce one line per entity. Printing hundreds of them buries
    the summary that follows, so the terminal gets a sample and the output file
    keeps the full list.
    """
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"... and {len(lines) - limit} more (full list in the --output file)"]


def build_clone_summary(source_entities: list[dict[str, Any]], removed_macer: list[dict[str, Any]],
                        created: list[dict[str, Any]], copied_documents: list[dict[str, Any]],
                        failed_documents: list[dict[str, Any]], verification_warnings: list[str],
                        args: argparse.Namespace) -> dict[str, Any]:
    """Reconcile what the source held against what the clone produced.

    Per entity type: how many the source had, how many were deliberately skipped
    as MACER, how many were cloned, and whether those add up. `balanced` false on
    any row means entities went missing without anything reporting a failure.
    """
    source_counts: dict[str, int] = {}
    for entity in source_entities + removed_macer:
        source_counts[str(entity.get("entity_type") or "Unknown")] = \
            source_counts.get(str(entity.get("entity_type") or "Unknown"), 0) + 1
    skipped_counts: dict[str, int] = {}
    for entity in removed_macer:
        skipped_counts[str(entity.get("entity_type") or "Unknown")] = \
            skipped_counts.get(str(entity.get("entity_type") or "Unknown"), 0) + 1
    cloned_counts: dict[str, int] = {}
    for item in created:
        cloned_counts[str(item.get("entity_type") or "Unknown")] = \
            cloned_counts.get(str(item.get("entity_type") or "Unknown"), 0) + 1

    by_type = []
    for entity_type in sorted(source_counts):
        in_source = source_counts[entity_type]
        skipped = skipped_counts.get(entity_type, 0)
        cloned = cloned_counts.get(entity_type, 0)
        by_type.append({
            "entity_type": entity_type, "in_source": in_source,
            "skipped_macer": skipped, "cloned": cloned,
            "balanced": cloned + skipped == in_source,
        })

    unbalanced = [row["entity_type"] for row in by_type if not row["balanced"]]
    reused = sum(1 for record in copied_documents if record.get("reused"))
    fresh = sum(1 for record in copied_documents if record.get("target_id") and not record.get("reused"))
    return {
        "entities": {
            "in_source_total": sum(source_counts.values()),
            "skipped_macer_total": len(removed_macer),
            "cloned_total": len(created),
            "by_type": by_type,
            "unbalanced_types": unbalanced,
        },
        "documents": {
            "requested": len(copied_documents) + len(failed_documents),
            "newly_copied": fresh,
            "already_present_reused": reused,
            "failed": len(failed_documents),
            "failed_filenames": [record.get("filename") for record in failed_documents],
        },
        "validation": {
            "carried_warnings": len(verification_warnings),
            "allow_validation_warnings": args.allow_validation_warnings,
        },
        "ok": not unbalanced and not failed_documents,
    }


def print_clone_summary(summary: dict[str, Any]) -> None:
    """Human-readable reconciliation, to stderr so it stays out of the JSON."""
    entities, documents = summary["entities"], summary["documents"]
    out = sys.stderr
    print("\n" + "=" * 68, file=out)
    print("CLONE SUMMARY", file=out)
    print("=" * 68, file=out)
    print(f"\nEntities   source {entities['in_source_total']}"
          f"  =  cloned {entities['cloned_total']}"
          f"  +  skipped as MACER {entities['skipped_macer_total']}", file=out)
    print(f"\n  {'entity type':<34}{'source':>8}{'cloned':>8}{'macer':>8}   ok", file=out)
    print("  " + "-" * 64, file=out)
    for row in entities["by_type"]:
        print(f"  {row['entity_type']:<34}{row['in_source']:>8}{row['cloned']:>8}"
              f"{row['skipped_macer']:>8}   {'yes' if row['balanced'] else 'NO'}", file=out)
    print(f"\nDocument files  requested {documents['requested']}"
          f"  newly copied {documents['newly_copied']}"
          f"  already present {documents['already_present_reused']}"
          f"  failed {documents['failed']}", file=out)
    print("  (document FILES, a different store from the entity counts above)", file=out)
    for name in documents["failed_filenames"]:
        print(f"    FAILED: {name}", file=out)
    carried = summary["validation"]["carried_warnings"]
    if carried:
        print(f"\nValidation {carried} warning(s) carried over from the source loan "
              f"(allowed by --allow-validation-warnings)", file=out)
    if entities["unbalanced_types"]:
        print(f"\nCHECK THESE: counts do not reconcile for "
              f"{', '.join(entities['unbalanced_types'])}", file=out)
    print(f"\nResult: {'OK' if summary['ok'] else 'COMPLETED WITH PROBLEMS'}", file=out)
    print("=" * 68 + "\n", file=out)


def nested_resource_id(value: Any) -> str | None:
    """Extract an id/document_id from an upload response envelope."""
    if isinstance(value, dict):
        for key in ("id", "document_id", "documentId"):
            if value.get(key):
                return str(value[key])
        for child in value.values():
            found = nested_resource_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = nested_resource_id(child)
            if found:
                return found
    return None


def document_source_id(document: dict[str, Any]) -> str | None:
    metadata = document.get("meta_data") or document.get("metadata") or document.get("metaData")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = None
    if isinstance(metadata, dict) and metadata.get("source_document_id"):
        return str(metadata["source_document_id"])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Clone a JazzX loan into a new project using one script.")
    parser.add_argument("--reference-loan-number", help="Existing loan number to clone")
    parser.add_argument("--target-loan-number", required=True, help="New loan number, e.g. 1441010_1")
    parser.add_argument("--source-collection-id", help="Known source loan collection ID")
    parser.add_argument("--target-collection-id", help="Existing target collection ID when resuming a project")
    parser.add_argument("--dashboard-collection-id", help="Collection containing LoanProject entities")
    parser.add_argument("--source-file", type=Path, help="Offline source JSON for repeatable dry runs")
    parser.add_argument("--project-name", help="New Assistant project name (default: clone_<target loan>)")
    parser.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--user-session-id", default=os.getenv("JAZZX_USER_SESSION_ID"),
                        help="Current POC2 x-user-session-id, if your environment requires one")
    parser.add_argument("--user-agent", default=os.getenv("JAZZX_USER_AGENT", "JazzX-Loan-Clone-Automation/1.0"))
    parser.add_argument("--timeout", type=float, default=180,
                        help="Per-request timeout for JSON API calls, in seconds. Listing a large "
                             "collection's documents or entities can genuinely exceed a minute")
    parser.add_argument("--document-timeout", type=float, default=600,
                        help="Wall-clock deadline for one document's download or upload, in seconds. "
                             "A document exceeding it is reported as failed and the run continues")
    parser.add_argument("--allow-duplicate-documents", action="store_true",
                        help="Copy documents even when none in the target could be matched to a "
                             "source document. Without this the run stops rather than duplicate "
                             "an entire collection")
    parser.add_argument("--workers", type=int, default=4,
                        help="Documents copied in parallel (default 4). Use 1 for the old serial "
                             "behaviour; raise it only if the gateway keeps up")
    parser.add_argument("--dry-run", action="store_true", help="Do not create or patch anything")
    parser.add_argument(
        "--copy-documents",
        action="store_true",
        help="Copy source and split document files into the target collection without waiting for processing",
    )
    parser.add_argument(
        "--remove-macer",
        action="store_true",
        help="Exclude MACER workflow entities from the clone",
    )
    parser.add_argument("--validate-only", action="store_true", help="Run source validation and exit without creating a project")
    parser.add_argument("--allow-validation-warnings", action="store_true", help="Create anyway when source JSON has validation warnings")
    parser.add_argument("--output", type=Path, help="Write the JSON result to this file")
    args = parser.parse_args()

    args.source_collection_id = validate_uuid(args.source_collection_id, "--source-collection-id")
    args.target_collection_id = validate_uuid(args.target_collection_id, "--target-collection-id")
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
    client: JazzXClient | None = None
    source_documents: list[dict[str, Any]] = []
    if args.source_file:
        if args.copy_documents:
            parser.error("--copy-documents requires a live source collection, not --source-file")
        source_collection_id, entities = read_source_file(args.source_file)
        if source_collection_id:
            source_collection_id = validate_uuid(str(source_collection_id), "source-file collection_id")
    else:
        client = JazzXClient(args.gateway_url, args.token, args.user_session_id, args.user_agent,
                             args.timeout, args.document_timeout)
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
        if args.copy_documents:
            source_documents = client.documents_in_collection(source_collection_id)
            print(f"Source documents found: {len(source_documents)}", file=sys.stderr)
        # LoanProject lives in the dashboard collection, not the loan collection.
        if dashboard_project and not any(entity.get("id") == dashboard_project.get("id") for entity in entities):
            entities.insert(0, dashboard_project)

    removed_macer_entities: list[dict[str, Any]] = []
    if args.remove_macer:
        entities, removed_macer_entities = remove_macer_entities(entities)
        print(
            f"MACER removal enabled: excluding {len(removed_macer_entities)} entities",
            file=sys.stderr,
        )

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
    resumed_project = False
    existing_target_entities: dict[str, dict[str, Any]] = {}
    copied_documents: list[dict[str, Any]] = []
    failed_documents: list[dict[str, Any]] = []
    document_id_mapping: dict[str, str] = {}
    if args.dry_run:
        project = {"id": f"dry-run-project-{args.target_loan_number}",
                   "collection_id": f"dry-run-collection-{args.target_loan_number}"}
        client = None
    else:
        client = JazzXClient(args.gateway_url, args.token, args.user_session_id, args.user_agent,
                             args.timeout, args.document_timeout)
        project_name = (args.project_name or f"clone_{args.target_loan_number}").strip()
        if not project_name or len(project_name) > 255 or any(character in project_name for character in "\r\n"):
            parser.error("--project-name cannot be blank, exceed 255 characters, or contain newlines")
        project, resumed_project = client.get_or_create_project(
            project_name, description, args.target_collection_id
        )
        if not project.get("collection_id"):
            raise RuntimeError(f"Project creation did not return collection_id: {project}")
        project["id"] = validate_uuid(str(project.get("id") or ""), "created project id")
        project["collection_id"] = validate_uuid(
            str(project["collection_id"]), "created project collection_id"
        )
        if resumed_project:
            existing_entities = client.entities_in_collection(project["collection_id"])
            existing_target_entities = {
                str(entity.get("name")): entity
                for entity in existing_entities
                if entity.get("name") and entity.get("entity_type")
            }
            print(
                f"Resuming existing project {project_name!r}: found "
                f"{len(existing_target_entities)} existing entities",
                file=sys.stderr,
            )

    if args.copy_documents:
        if client is None:
            raise ValueError("--copy-documents requires a live source collection")
        if args.dry_run:
            copied_documents = [
                {
                    "source_id": str(document.get("id") or document.get("document_id") or ""),
                    "target_id": None,
                    "filename": client.document_filename(
                        document, str(document.get("id") or document.get("document_id") or "")
                    ),
                    "status": "dry-run; not uploaded",
                }
                for document in source_documents
            ]
            print(
                f"Documents that would be copied: {len(copied_documents)} (dry run; no uploads)",
                file=sys.stderr,
            )
            existing_documents = []
        else:
            existing_documents = client.documents_in_collection(project["collection_id"]) if resumed_project else []
        # Matching a target document back to the source document it came from.
        #
        # The reliable key is meta_data.source_document_id, written at upload. But
        # Knowledge Hub does not always return meta_data on the documents listing,
        # and when it does not, every target document looks unrelated, nothing is
        # recognised as already copied, and a resumed run uploads the whole set a
        # second time. That is how a 91-document collection became 182.
        #
        # So filename is kept as a fallback. Filenames are not unique -- a loan has
        # many identically named split children -- so they are held in a pool and
        # consumed one at a time: N target documents named X satisfy N source
        # documents named X, not all of them. Size breaks ties where available.
        existing_by_source_id: dict[str, dict[str, Any]] = {}
        filename_pool: dict[str, list[dict[str, Any]]] = {}
        for document in existing_documents:
            source_id_value = document_source_id(document)
            if source_id_value:
                existing_by_source_id[source_id_value] = document
            name = client.document_filename(document, "")
            if name:
                filename_pool.setdefault(name, []).append(document)
        match_lock = threading.Lock()
        matched_by_filename = 0

        def claim_existing(source_document_id: str, filename: str,
                           size: Any) -> dict[str, Any] | None:
            """Take the target document already holding this source document, if any."""
            nonlocal matched_by_filename
            with match_lock:
                found = existing_by_source_id.pop(source_document_id, None)
                if found is not None:
                    candidates = filename_pool.get(filename) or []
                    if found in candidates:
                        candidates.remove(found)      # cannot also be claimed by name
                    return found
                candidates = filename_pool.get(filename)
                if not candidates:
                    return None
                index = 0
                if size is not None:
                    index = next((i for i, c in enumerate(candidates)
                                  if c.get("size") == size), 0)
                matched_by_filename += 1
                return candidates.pop(index)

        total_documents = len(source_documents)
        progress = itertools.count(1)
        progress_lock = threading.Lock()

        def copy_one(source_document: dict[str, Any]) -> dict[str, Any]:
            """Copy one document. Never raises: a failure is returned as data."""
            with progress_lock:
                index = next(progress)
            source_document_id = str(source_document.get("id") or source_document.get("document_id") or "")
            filename = client.document_filename(source_document, source_document_id or f"index-{index}")
            record: dict[str, Any] = {
                "source_id": source_document_id, "target_id": None,
                "filename": filename, "status": None, "error": None,
            }
            if not source_document_id:
                record["error"] = f"source document at position {index} has no id"
                return record
            existing_document = claim_existing(
                source_document_id, filename, source_document.get("size")
            )
            try:
                if existing_document is not None:
                    response: Any = existing_document
                    target_document_id = str(
                        existing_document.get("id") or existing_document.get("document_id") or ""
                    )
                    record["reused"] = True
                else:
                    print(f"Copying document {index}/{total_documents}: {filename}", file=sys.stderr)
                    content, _ = client.download_document(
                        source_document, source_document_id, source_collection_id
                    )
                    response = client.upload_document(
                        project["collection_id"], filename, content, source_document_id
                    )
                    target_document_id = nested_resource_id(response) or ""
                if not target_document_id:
                    raise RuntimeError(f"upload returned no target document id: {response}")
            except Exception as exc:  # one bad document must not end the run
                record["error"] = str(exc)
                print(
                    f"FAILED document {index}/{total_documents} ({filename}): {exc}",
                    file=sys.stderr,
                )
                return record
            record["target_id"] = target_document_id
            record["status"] = response.get("status") if isinstance(response, dict) else None
            return record

        # If the target already holds documents but not one of them can be matched,
        # every upload below would be a duplicate rather than a resume. Stop instead:
        # this is the failure that silently doubled a collection, and it is cheaper
        # to refuse than to clean up afterwards.
        matchable = len(existing_by_source_id) + sum(len(v) for v in filename_pool.values())
        if existing_documents and matchable == 0 and not args.allow_duplicate_documents:
            raise RuntimeError(
                f"The target collection already holds {len(existing_documents)} documents, but none "
                f"could be matched to a source document by meta_data.source_document_id or by "
                f"filename. Copying now would duplicate every one of them.\n"
                f"Inspect the target listing, or pass --allow-duplicate-documents to copy anyway."
            )

        pending = [document for document in source_documents if not args.dry_run]
        results: list[dict[str, Any]] = []
        if pending:
            workers = max(1, args.workers)
            if workers == 1:
                results = [copy_one(document) for document in pending]
            else:
                print(f"Copying {len(pending)} documents with {workers} workers", file=sys.stderr)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    results = list(pool.map(copy_one, pending))

        for record in results:
            if record["error"]:
                failed_documents.append(record)
            else:
                document_id_mapping[record["source_id"]] = record["target_id"]
                copied_documents.append({
                    "source_id": record["source_id"], "target_id": record["target_id"],
                    "filename": record["filename"], "status": record["status"],
                    "reused": bool(record.get("reused")),
                })
        print(
            f"Documents copied/reused: {len(copied_documents)}; processing is asynchronous and was not awaited",
            file=sys.stderr,
        )
        if failed_documents:
            print(
                f"\n{len(failed_documents)} document(s) FAILED to copy and were skipped:",
                file=sys.stderr,
            )
            for record in failed_documents:
                print(f"  - {record['filename']} ({record['source_id']}): {record['error']}", file=sys.stderr)
            print(
                "These ids are absent from the id mapping, so entities referring to them keep the\n"
                "SOURCE document id and point outside the new collection. Re-run to retry just these;\n"
                "documents already copied are skipped. Full list in the --output file.",
                file=sys.stderr,
            )

    # Placeholders avoid accidental source IDs in circular references during POST.
    source_ids = {str(entity["id"]) for entity in entities}
    id_mapping: dict[str, str] = dict(document_id_mapping)
    # Placeholders stand in for entities that do not exist yet, and are replaced by
    # the patch pass. They must be RFC-4122 UUIDs: ontologies validate id fields
    # against that pattern, and a "pending-reference-..." string is rejected with a
    # 422 before the entity is ever created. The fixed prefix keeps them
    # recognisable -- version nibble 4 and variant 8 satisfy the pattern -- and the
    # counter suffix guarantees uniqueness without risking a random collision.
    placeholders = {
        source_id: f"{PLACEHOLDER_UUID_PREFIX}{index:012x}"
        for index, source_id in enumerate(sorted(source_ids))
    }
    created: list[dict[str, Any]] = []
    original_json: dict[str, dict[str, Any]] = {}
    expected_json: dict[str, dict[str, Any]] = {}
    # What each entity's json_value actually holds right now: the payload just
    # POSTed, or what a resumed run found already stored. Lets the patch pass
    # skip entities whose stored value is already final.
    stored_json: dict[str, dict[str, Any]] = {}
    for source in entity_order(entities):
        source_id = str(source["id"])
        payload = clone_payload(source, project["collection_id"], args.target_loan_number, project["id"], id_mapping, placeholders)
        original_json[source_id] = deepcopy(source.get("json_value") or {})
        existing = existing_target_entities.get(payload["name"])
        if existing is not None and existing.get("entity_type") != source.get("entity_type"):
            raise RuntimeError(
                f"Existing target entity name collision for {payload['name']!r}: "
                f"found {existing.get('entity_type')}, expected {source.get('entity_type')}"
            )
        if existing is not None:
            response = existing
            id_mapping[source_id] = str(response["id"])
            # Already in the target from an earlier run; its stored value may
            # already be the final one, in which case the patch below is a no-op.
            stored_json[source_id] = existing.get("json_value") or {}
        elif args.dry_run:
            response = {"id": f"dry-run-{source_id}"}
            stored_json[source_id] = payload["json_value"]
        else:
            response = client.create_entity(payload)
            stored_json[source_id] = payload["json_value"]
        if not response.get("id"):
            raise RuntimeError(f"Creation of {source_id} returned no entity id: {response}")
        if source_id not in id_mapping:
            id_mapping[source_id] = str(response["id"])
        created.append({"source_id": source_id, "target_id": id_mapping[source_id], "entity_type": source.get("entity_type")})

    # Patch from the source truth once all actual IDs are known. Entities can refer
    # to each other circularly, so no creation order makes every reference known up
    # front; creation substitutes placeholders and this pass resolves them.
    #
    # An entity whose stored value already satisfies the target needs no patch: it
    # either had no references to resolve, or a previous run already patched it.
    # The test is json_contains -- the same predicate verify_clone uses -- so
    # anything skipped here is guaranteed to pass verification.
    if not args.dry_run:
        needs_patch: list[tuple[str, dict[str, Any]]] = []
        for source_id, json_value in original_json.items():
            target_json = rewrite_loan_identity(
                replace_ids(json_value, id_mapping), args.target_loan_number,
                project["id"], project["collection_id"]
            )
            expected_json[id_mapping[source_id]] = target_json
            if not json_contains(stored_json.get(source_id), target_json):
                needs_patch.append((id_mapping[source_id], target_json))

        skipped = len(original_json) - len(needs_patch)
        print(
            f"Patching {len(needs_patch)} of {len(original_json)} entities "
            f"({skipped} already final)",
            file=sys.stderr,
        )
        if needs_patch:
            patch_workers = max(1, args.workers)
            # Each patch sets one entity's whole json_value and touches nothing
            # else, so they are independent and safe to run concurrently.
            if patch_workers == 1:
                for target_id, target_json in needs_patch:
                    client.patch_entity_json(target_id, target_json)
            else:
                with ThreadPoolExecutor(max_workers=patch_workers) as pool:
                    list(pool.map(lambda item: client.patch_entity_json(*item), needs_patch))
        print(f"Patched {len(needs_patch)} entities", file=sys.stderr)
    if args.dry_run:
        # This is the JSON that would be persisted after every reference is resolved.
        for source_id, json_value in original_json.items():
            expected_json[id_mapping[source_id]] = rewrite_loan_identity(
                replace_ids(json_value, id_mapping), args.target_loan_number, project["id"], project["collection_id"]
            )

    verification_failures: list[str] = []
    verification_warnings: list[str] = []
    if not args.dry_run:
        verification_failures, verification_warnings = verify_clone(
            client, project["collection_id"], created, source_ids | set(document_id_mapping),
            expected_json, args.allow_validation_warnings,
            excluded_ids={str(entity["id"]) for entity in removed_macer_entities if entity.get("id")},
        )
    if verification_warnings:
        print(
            f"\nTARGET VALIDATION WARNINGS ({len(verification_warnings)}), carried over from the "
            "source loan and allowed by --allow-validation-warnings:\n- "
            + "\n- ".join(summarise_lines(verification_warnings)),
            file=sys.stderr,
        )
    if verification_failures:
        # The full list can run to hundreds of lines. The terminal gets a readable
        # sample; the rest is written out first so the hint below is true even
        # though this path ends in an exception.
        result_hint = ""
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({
                "verification": "failed",
                "target_collection_id": project["collection_id"],
                "target_project_id": project.get("id"),
                "verification_failure_count": len(verification_failures),
                "verification_failures": verification_failures,
                "verification_warnings": verification_warnings,
            }, indent=2) + "\n", encoding="utf-8")
            result_hint = f"\nAll {len(verification_failures)} written to {args.output}"
        raise RuntimeError(
            f"Clone verification failed ({len(verification_failures)} problems):\n- "
            + "\n- ".join(summarise_lines(verification_failures)) + result_hint
        )

    result = {"source_collection_id": source_collection_id, "target_project_id": project.get("id"),
              "target_collection_id": project["collection_id"], "reference_loan_number": args.reference_loan_number,
              "target_loan_number": args.target_loan_number, "dry_run": args.dry_run,
              "resumed_project": resumed_project,
              "remove_macer": args.remove_macer,
              "copy_documents": args.copy_documents,
              "copied_document_count": len(copied_documents),
              "copied_documents": copied_documents,
              "failed_document_count": len(failed_documents),
              "failed_documents": failed_documents,
              "verification_warnings": verification_warnings,
              "removed_macer_count": len(removed_macer_entities),
              "removed_macer_entity_types": sorted({str(entity.get("entity_type")) for entity in removed_macer_entities}),
              "created_count": len(created), "created_entities": created, "id_mapping": id_mapping,
              "validation_warnings": warnings, "verification": "skipped (dry run)" if args.dry_run else "passed"}
    result["summary"] = build_clone_summary(
        entities, removed_macer_entities, created, copied_documents, failed_documents,
        verification_warnings, args,
    )
    print_clone_summary(result["summary"])
    encoded = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        # The full result is mostly id_mapping -- one line per cloned entity, a few
        # hundred KB of UUID pairs. It belongs in the file, not scrolled past on a
        # terminal, so stdout gets the summary and a pointer instead.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
        print(f"Full result ({len(encoded) // 1024} KB, including the "
              f"{len(id_mapping)}-entry id mapping) written to {args.output}")
    else:
        # Nowhere to write it, so emit it: the caller may be piping into jq.
        print(encoded)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
