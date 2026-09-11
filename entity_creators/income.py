"""
Income entity payload builder.
"""

import logging
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from .base import (
    INCOME_SOURCE_ONTOLOGY_ID,
    build_provenance,
    generate_stable_uuid,
    get_current_timestamp,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Income Type Literals - Matches MISMO IncomeSource ontology schema
# ============================================================================

IncomeType = Literal[
    "Base",
    "Overtime",
    "Bonus",
    "Commission",
    "MilitaryBasePay",
    "MilitaryRationsAllowance",
    "MilitaryFlightPay",
    "MilitaryHazardPay",
    "MilitaryClothesAllowance",
    "MilitaryQuartersAllowance",
    "MilitaryPropPay",
    "MilitaryOverseasPay",
    "MilitaryCombatPay",
    "MilitaryVariableHousingAllowance",
    "SelfEmployment",
    "SocialSecurity",
    "Pension",
    "Retirement",
    "Disability",
    "ChildSupport",
    "Alimony",
    "RentalIncome",
    "InterestDividends",
    "NotesReceivable",
    "Trust",
    "OtherIncome",
    "AutomobileAllowance",
    "BoarderIncome",
    "CapitalGains",
    "EmploymentRelatedAssets",
    "FosterCare",
    "HousingAllowance",
    "MortgageCreditCertificate",
    "MortgageDifferential",
    "PublicAssistance",
    "RoyaltyPayment",
    "SeasonalIncome",
    "SecondaryEmployment",
    "TemporaryLeave",
    "TipIncome",
    "UnemploymentBenefits",
    "VABenefits",
    "AccessoryUnitIncome",
]

IncomeFrequency = Literal[
    "Weekly",
    "BiWeekly",
    "SemiMonthly",
    "Monthly",
    "Quarterly",
    "SemiAnnually",
    "Annually",
]

DocumentationLevel = Literal[
    "FullDocumentation",
    "ReducedDocumentation",
    "NoDocumentation",
    "Stated",
]

VerificationStatus = Literal[
    "NotVerified",
    "VerbalVOE",
    "WrittenVOE",
    "PaystubVerified",
    "TaxReturnVerified",
    "BankStatementVerified",
]


# ============================================================================
# JazzIncomeSource Model - MISMO IncomeSource entity ontology schema
# ============================================================================


class JazzIncomeSource(BaseModel):
    """
    Jazz IncomeSource entity with validation.

    This uses Pydantic's validation and transformation capabilities
    to validate income data before sending to the API.
    Follows the MISMO IncomeSource entity ontology schema.
    """

    # Required fields
    id: str
    borrower_id: str
    income_type: IncomeType

    # Optional fields
    employment_id: Optional[str] = Field(
        None,
        description="Reference to EmploymentRecord entity (set after employment sync)",
    )
    is_employment_income: Optional[bool] = Field(
        None,
        description="True if this income originated from Employment-type income in LOS",
    )
    monthly_amount_usd: Optional[float] = Field(None, ge=0)
    frequency: Optional[IncomeFrequency] = None
    is_primary: Optional[bool] = None
    documentation_level: Optional[DocumentationLevel] = None
    verification_status: Optional[VerificationStatus] = None

    # Provenance (optional, typically added by system)
    provenance: Optional[dict] = None


class IncomePayloadBuilder:
    """Builds payload for IncomeSource entities."""

    @staticmethod
    def build_all(
        collection_id: str,
        input_data: Dict[str, Any],
        borrower_mappings: List[Dict[str, str]] = None,
        description: str = "Income source entity",
    ) -> List[Tuple[Dict[str, Any], str, str]]:
        """Build payloads for all incomes across all borrowers.

        Args:
            collection_id: The collection ID from project creation.
            input_data: The full input JSON data.
            borrower_mappings: List of dicts with 'internal_id' and 'external_id' keys.
            description: Description for the entities.

        Returns:
            A list of tuples (payload, external_id, borrower_id) for each income.
        """
        borrowers = input_data.get("borrowers", [])
        results = []
        for bidx, borrower in enumerate(borrowers):
            # Resolve borrower internal_id from mappings
            borrower_internal_id = None
            if borrower_mappings and bidx < len(borrower_mappings) and borrower_mappings[bidx]:
                borrower_internal_id = borrower_mappings[bidx].get("internal_id")
            if not borrower_internal_id:
                borrower_internal_id = generate_stable_uuid(collection_id, "Borrower", bidx)

            for income_idx, income in enumerate(borrower.get("incomes", [])):
                try:
                    payload, external_id = IncomePayloadBuilder.build_single(
                        collection_id=collection_id,
                        income=income,
                        borrower_internal_id=borrower_internal_id,
                        description=description,
                        external_income_id=generate_stable_uuid(
                            collection_id,
                            "IncomeSource",
                            borrower_internal_id,
                            income.get("id") or income.get("incomeId") or f"{income.get('incomeType', 'Base')}:{income_idx}",
                        ),
                    )
                    results.append((payload, external_id, borrower_internal_id))
                except ValidationError as ve:
                    logger.error(
                        f"Income validation failed for borrower {borrower_internal_id}: {ve.errors()}"
                    )
                    raise
        return results

    @staticmethod
    def build_single(
        collection_id: str,
        income: Dict[str, Any],
        borrower_internal_id: str,
        description: str = "Income source entity",
        external_income_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Build the payload for a single IncomeSource entity.

        Args:
            collection_id: The collection ID from project creation.
            income: The income data from input.
            borrower_internal_id: The internal ID of the borrower.
            description: Description for the entity.

        Returns:
            A tuple of (payload dict, external_income_id).

        Raises:
            ValidationError: If the income data fails Pydantic validation.
        """
        now = get_current_timestamp()
        external_income_id = external_income_id or generate_stable_uuid(
            collection_id,
            "IncomeSource",
            borrower_internal_id,
            income.get("id") or income.get("incomeId") or income.get("incomeType", "Base"),
        )
        income_type = income.get("incomeType", "Base")
        entity_name = f"Income-{income_type}-{external_income_id[:8]}"

        # --- Compute monthly amount ---
        # Priority: totalCalculatedQualifiedMonthlyIncome > qualifiedEmploymentBaseIncome > statedEmploymentBaseIncome
        monthly_amount = income.get("totalCalculatedQualifiedMonthlyIncome")
        if monthly_amount is None:
            qualified = income.get("qualifiedEmploymentBaseIncome") or {}
            if qualified.get("amount"):
                monthly_amount = qualified["amount"]
        if monthly_amount is None:
            stated = income.get("statedEmploymentBaseIncome") or {}
            if stated.get("amount"):
                monthly_amount = stated["amount"]

        # --- Determine if this is employment income ---
        is_employment_income = income.get("isEmploymentIncome", False)
        if not is_employment_income and income.get("employer"):
            is_employment_income = True

        # --- Build and validate JazzIncomeSource using Pydantic ---
        validated_income = JazzIncomeSource(
            id=external_income_id,
            borrower_id=borrower_internal_id,
            income_type=income_type,
            monthly_amount_usd=float(monthly_amount) if monthly_amount else None,
            frequency=income.get("frequency"),
            is_primary=income.get("isPrimary"),
            is_employment_income=is_employment_income if is_employment_income else None,
            documentation_level=income.get("documentationLevel"),
            verification_status=income.get("verificationStatus"),
            provenance=build_provenance(),
        )

        # Use mode='json' to serialize properly, exclude_none to skip None values
        # Exclude is_employment_income as it's not in the ontology (internal tracking only)
        json_value = validated_income.model_dump(
            mode="json", exclude_none=True, exclude={"is_employment_income"}
        )

        payload = {
            "entity_type": "IncomeSource",
            "ontology_id": INCOME_SOURCE_ONTOLOGY_ID,
            "name": entity_name,
            "collection_id": collection_id,
            "json_value": json_value,
            "description": description,
        }

        return payload, external_income_id
