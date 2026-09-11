"""
EmploymentRecord entity payload builder.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from .base import (
    EMPLOYMENT_RECORD_ONTOLOGY_ID,
    build_provenance,
    generate_stable_uuid,
    get_current_timestamp,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Address Model - Reusable address component
# ============================================================================


class EmployerAddress(BaseModel):
    """Standard US address format for employer."""

    street_line_1: Optional[str] = Field(None, max_length=255)
    street_line_2: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, description="US State code")
    postal_code: Optional[str] = None
    county: Optional[str] = Field(None, max_length=100)
    country: str = Field(default="US")


# ============================================================================
# OwnershipPercentage Model - Matches ontology self_employed_ownership_percent
# ============================================================================


class OwnershipPercentage(BaseModel):
    """Ownership percentage with optional qualifier (matches ontology)."""

    value: Optional[float] = Field(None, ge=0, le=100)
    qualifier: Optional[Literal["Exactly", "MoreThan", "LessThan", "AtLeast", "AtMost"]] = None


# ============================================================================
# Employment Type Literals
# ============================================================================

EmploymentType = Literal["Current", "Previous", "Secondary"]

EmploymentStatus = Literal[
    "Employed",
    "SelfEmployed",
    "Retired",
    "NotEmployed",
    "Military",
    "IndependentContractor",
]


# ============================================================================
# JazzEmploymentRecord Model - MISMO EmploymentRecord entity ontology schema
# ============================================================================


class JazzEmploymentRecord(BaseModel):
    """
    Jazz EmploymentRecord entity with validation.

    This uses Pydantic's validation and transformation capabilities
    to validate employment data before sending to the API.
    Follows the MISMO EmploymentRecord entity ontology schema.
    """

    # Required fields
    id: str
    borrower_id: str
    employment_type: EmploymentType

    # Source income reference (Employment is derived from Employment-type income)
    source_income_id: Optional[str] = Field(
        None,
        description="Internal ID of the IncomeSource entity this employment was derived from",
    )

    # Employer information
    employer_name: Optional[str] = Field(None, max_length=255)
    employer_address: Optional[EmployerAddress] = None
    employer_phone: Optional[str] = None

    # Position details
    position_title: Optional[str] = Field(None, max_length=100)
    start_date: Optional[str] = Field(None, description="Date in YYYY-MM-DD format")
    end_date: Optional[str] = Field(None, description="Date in YYYY-MM-DD format")

    # Duration
    years_in_profession: Optional[float] = Field(None, ge=0)
    months_on_job: Optional[int] = Field(None, ge=0)

    # Self-employment details
    is_self_employed: Optional[bool] = None
    self_employed_ownership_percent: Optional[OwnershipPercentage] = None
    is_employed_by_family_member: Optional[bool] = None

    # Employment status
    employment_status: Optional[EmploymentStatus] = None

    # Income
    gross_monthly_income_usd: Optional[float] = Field(None, ge=0)

    # Provenance (optional, typically added by system)
    provenance: Optional[dict] = None


class EmploymentPayloadBuilder:
    """Builds payload for EmploymentRecord entities.

    Employment records are extracted from income data (income.employer).
    The employment external_id is the same as the corresponding income external_id
    to maintain the link between income and employment.
    """

    @staticmethod
    def build_all(
        collection_id: str,
        input_data: Dict[str, Any],
        borrower_mappings: List[Dict[str, str]] = None,
        income_external_ids: List[str] = None,
        description: str = "Employment record entity",
    ) -> List[Tuple[Dict[str, Any], str, str]]:
        """Build payloads for all employment records across all borrowers.

        Args:
            collection_id: The collection ID from project creation.
            input_data: The full input JSON data.
            borrower_mappings: List of dicts with 'internal_id' and 'external_id' keys.
            income_external_ids: List of income external IDs to use as employment IDs.
            description: Description for the entities.

        Returns:
            A list of tuples (payload, external_id, borrower_id) for each employment.
        """
        borrowers = input_data.get("borrowers", [])
        results = []
        income_idx = 0

        for bidx, borrower in enumerate(borrowers):
            # Get borrower mapping
            borrower_external_id = None
            if borrower_mappings and bidx < len(borrower_mappings) and borrower_mappings[bidx]:
                borrower_external_id = borrower_mappings[bidx].get("external_id")
            if not borrower_external_id:
                borrower_external_id = generate_stable_uuid(collection_id, "Borrower", bidx)

            for income in borrower.get("incomes", []):
                employer = income.get("employer") or {}
                if not employer:
                    income_idx += 1
                    continue

                # Get the income external_id to use as employment external_id (maintaining link)
                income_external_id = None
                if income_external_ids and income_idx < len(income_external_ids):
                    income_external_id = income_external_ids[income_idx]
                if not income_external_id:
                    income_external_id = generate_stable_uuid(
                        collection_id, "EmploymentRecord", borrower_external_id, income_idx
                    )

                try:
                    payload, external_id = EmploymentPayloadBuilder.build_single(
                        collection_id=collection_id,
                        employer=employer,
                        income=income,
                        borrower_external_id=borrower_external_id,
                        employment_external_id=income_external_id,
                        description=description,
                    )
                    results.append((payload, external_id, borrower_external_id))
                except ValidationError as ve:
                    logger.error(
                        f"Employment validation failed for borrower {borrower_external_id}: {ve.errors()}"
                    )
                    raise
                income_idx += 1

        return results

    @staticmethod
    def build_single(
        collection_id: str,
        employer: Dict[str, Any],
        income: Dict[str, Any],
        borrower_external_id: str,
        employment_external_id: str,
        description: str = "Employment record entity",
    ) -> Tuple[Dict[str, Any], str]:
        """Build the payload for a single EmploymentRecord entity.

        Args:
            collection_id: The collection ID from project creation.
            employer: The employer data from income.
            income: The income data.
            borrower_external_id: The external ID of the borrower.
            employment_external_id: The external ID for the employment (same as income).
            description: Description for the entity.

        Returns:
            A tuple of (payload dict, employment_external_id).

        Raises:
            ValidationError: If the employment data fails Pydantic validation.
        """
        now = get_current_timestamp()

        # Employment type (Current, Previous, etc.)
        employment_type = employer.get("employmentClassificationType", "Current")
        if employment_type == "Primary":
            employment_type = "Current"

        entity_name = f"Employment-{employment_type}-{employment_external_id[:8]}"

        # --- Employer address mapping using Pydantic model ---
        input_addr = employer.get("address") or {}
        employer_address = None
        if input_addr:
            employer_address = EmployerAddress(
                city=input_addr.get("city"),
                state=input_addr.get("stateCode", input_addr.get("state")),
                country=input_addr.get("country", "US"),
                postal_code=input_addr.get("zipCode", input_addr.get("zip")),
                street_line_1=input_addr.get("line", input_addr.get("street")),
                street_line_2=input_addr.get("line2"),
                county=input_addr.get("county"),
            )

        # --- Calculate months on job ---
        start_date = employer.get("startDate", "")
        months_on_job = employer.get("numberOfMonthsInThisLineOfWork", 0)
        if start_date and not months_on_job:
            try:
                start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                now_dt = datetime.now(timezone.utc)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                months_on_job = int((now_dt - start_dt).days / 30.44)
            except (ValueError, AttributeError):
                pass

        # --- Years in profession ---
        years_in_profession = months_on_job / 12.0 if months_on_job else 0

        # --- Gross monthly income ---
        gross_monthly = income.get("totalCalculatedQualifiedMonthlyIncome", 0)
        if not gross_monthly:
            qualified = income.get("qualifiedEmploymentBaseIncome") or {}
            gross_monthly = qualified.get("amount", 0)
        if not gross_monthly:
            stated = income.get("statedEmploymentBaseIncome") or {}
            gross_monthly = stated.get("amount", 0)

        # --- Build and validate JazzEmploymentRecord using Pydantic ---
        validated_employment = JazzEmploymentRecord(
            id=employment_external_id,
            borrower_id=borrower_external_id,
            employer_name=employer.get("name"),
            job_title=employer.get("jobTitle"),
            employment_type=employer.get("status", None),
            employment_status=employer.get("employmentStatus", "Employed"),
            is_self_employed=employer.get("isSelfEmployed", False),
            is_current=employer.get("status") == "Current",
            start_date=employer.get("startDate"),
            end_date=employer.get("endDate"),
            employer_address=employer_address,
            employer_phone=employer.get("phone"),
            months_in_profession=employer.get("numberOfMonthsInThisLineOfWork"),
            is_employed_by_family=employer.get("isEmployedByFamilyMember", False),
            provenance=build_provenance(),
        )

        # Use mode='json' to serialize properly, exclude_none to skip None values
        json_value = validated_employment.model_dump(mode="json", exclude_none=True)

        payload = {
            "entity_type": "EmploymentRecord",
            "ontology_id": EMPLOYMENT_RECORD_ONTOLOGY_ID,
            "name": entity_name,
            "collection_id": collection_id,
            "json_value": json_value,
            "description": description,
        }

        return payload, employment_external_id
