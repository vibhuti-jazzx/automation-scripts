"""
Liability entity payload builder.
"""

import logging
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from .base import (
    LIABILITY_ONTOLOGY_ID,
    build_provenance,
    generate_stable_uuid,
    get_current_timestamp,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Liability Type Literal - Matches MISMO Liability ontology schema
# ============================================================================

LiabilityType = Literal[
    "Mortgage",
    "HELOC",
    "Installment",
    "Revolving",
    "OpenThirtyDay",
    "LeasePayment",
    "ChildSupport",
    "Alimony",
    "SeparateMaintenanceExpense",
    "JobRelatedExpense",
    "Other",
    "CollectionsJudgments",
    "DeferredStudentLoan",
    "GovernmentStudentLoan",
    "Taxes",
    "MedicalDebt",
    "AutoLoan",
    "StudentLoan",
    "PersonalLoan",
]


# ============================================================================
# JazzLiability Model - MISMO Liability entity ontology schema
# ============================================================================


class JazzLiability(BaseModel):
    """
    Jazz Liability entity with validation.

    This uses Pydantic's validation and transformation capabilities
    to validate liability data before sending to the API.
    Follows the MISMO Liability entity ontology schema.
    """

    # Required fields
    id: str
    borrower_id: str
    liability_type: LiabilityType

    # Creditor details
    creditor_name: Optional[str] = Field(None, max_length=255)
    account_number_masked: Optional[str] = Field(None, max_length=20)

    # Balance and payment
    unpaid_balance_usd: Optional[float] = Field(None, ge=0)
    monthly_payment_usd: Optional[float] = Field(None, ge=0)
    months_remaining: Optional[int] = Field(None, ge=0)

    # Subject property lien details
    is_subject_property_lien: Optional[bool] = None
    lien_position: Optional[int] = Field(None, ge=1, le=4)

    # Closing and subordination
    will_be_paid_off_at_closing: Optional[bool] = None
    will_be_subordinated: Optional[bool] = None

    # DTI exclusion
    is_excluded_from_dti: Optional[bool] = None
    exclusion_reason: Optional[str] = None

    # Provenance (optional, typically added by system)
    provenance: Optional[dict] = None


class LiabilityPayloadBuilder:
    """Builds payload for Liability entities."""

    @staticmethod
    def build_all(
        collection_id: str,
        input_data: Dict[str, Any],
        borrower_mappings: List[Dict[str, str]] = None,
        description: str = "Liability entity",
    ) -> List[Tuple[Dict[str, Any], str, str]]:
        """Build payloads for all liabilities across all borrowers.

        Args:
            collection_id: The collection ID from project creation.
            input_data: The full input JSON data.
            borrower_mappings: List of dicts with 'internal_id' and 'external_id' keys.
            description: Description for the entities.

        Returns:
            A list of tuples (payload, external_id, borrower_id) for each liability.
        """
        borrowers = input_data.get("borrowers", [])
        results = []
        for bidx, borrower in enumerate(borrowers):
            borrower_internal_id = None
            if borrower_mappings and bidx < len(borrower_mappings) and borrower_mappings[bidx]:
                borrower_internal_id = borrower_mappings[bidx].get("internal_id")
            if not borrower_internal_id:
                borrower_internal_id = generate_stable_uuid(collection_id, "Borrower", bidx)

            for liability_idx, liability in enumerate(borrower.get("liabilities", [])):
                try:
                    payload, external_id = LiabilityPayloadBuilder.build_single(
                        collection_id=collection_id,
                        liability=liability,
                        borrower_internal_id=borrower_internal_id,
                        description=description,
                        external_liability_id=generate_stable_uuid(
                            collection_id,
                            "Liability",
                            borrower_internal_id,
                            liability.get("id") or liability.get("liabilityId") or liability.get("accountNumber") or liability_idx,
                        ),
                    )
                    results.append((payload, external_id, borrower_internal_id))
                except ValidationError as ve:
                    logger.error(
                        f"Liability validation failed for borrower {borrower_internal_id}: {ve.errors()}"
                    )
                    raise
        return results

    @staticmethod
    def build_single(
        collection_id: str,
        liability: Dict[str, Any],
        borrower_internal_id: str,
        description: str = "Liability entity",
        external_liability_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Build the payload for a single Liability entity.

        Args:
            collection_id: The collection ID from project creation.
            liability: The liability data from input.
            borrower_internal_id: The internal ID of the borrower.
            description: Description for the entity.

        Returns:
            A tuple of (payload dict, external_liability_id).

        Raises:
            ValidationError: If the liability data fails Pydantic validation.
        """
        now = get_current_timestamp()
        external_liability_id = external_liability_id or generate_stable_uuid(
            collection_id,
            "Liability",
            borrower_internal_id,
            liability.get("id") or liability.get("liabilityId") or liability.get("accountNumber") or liability.get("liabilityType", "Other"),
        )
        liability_type = liability.get("liabilityType", "Other")
        entity_name = f"Liability-{liability_type}-{external_liability_id[:8]}"

        # --- Build and validate JazzLiability using Pydantic ---
        validated_liability = JazzLiability(
            id=external_liability_id,
            borrower_id=borrower_internal_id,
            liability_type=liability_type,
            creditor_name=liability.get("creditorName") or None,
            account_number_masked=liability.get("accountNumber", liability.get("accountIdentifier")) or None,
            unpaid_balance_usd=liability.get("unpaidBalance"),
            monthly_payment_usd=liability.get("monthlyPaymentAmount", liability.get("monthlyPayment")),
            months_remaining=liability.get("monthsRemaining"),
            is_subject_property_lien=liability.get("isSubjectPropertyLien", False),
            lien_position=liability.get("lienPosition"),
            will_be_paid_off_at_closing=liability.get("willBePaidOffAtClosing", liability.get("paidOffAtClosing", False)),
            will_be_subordinated=liability.get("willBeSubordinated"),
            is_excluded_from_dti=liability.get("isExcludedFromDti", liability.get("excludeFromDti", False)),
            exclusion_reason=liability.get("exclusionReason"),
            provenance=build_provenance(),
        )

        # Use mode='json' to serialize properly, exclude_none to skip None values
        json_value = validated_liability.model_dump(mode="json", exclude_none=True)

        payload = {
            "entity_type": "Liability",
            "ontology_id": LIABILITY_ONTOLOGY_ID,
            "name": entity_name,
            "collection_id": collection_id,
            "json_value": json_value,
            "description": description,
        }

        return payload, external_liability_id
