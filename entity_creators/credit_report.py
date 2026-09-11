"""
CreditReport entity payload builder.
"""

import logging
from datetime import date
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from .base import (
    CREDIT_REPORT_ONTOLOGY_ID,
    build_provenance,
    generate_stable_uuid,
    get_current_timestamp,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Credit Score Model Literals
# ============================================================================

CreditBureau = Literal["Equifax", "Experian", "TransUnion"]

ScoreModel = Literal[
    "FICO8",
    "FICO9",
    "FICO10",
    "FICO10T",
    "FICOAuto",
    "FICOBankcard",
    "VantageScore3",
    "VantageScore4",
    "ClassicFICO",
]

ReportType = Literal["Individual", "Joint", "TriMerge", "SingleBureau"]


# ============================================================================
# Nested Models
# ============================================================================


class CreditScore(BaseModel):
    """Individual credit bureau score - matches ontology CreditScore schema."""

    bureau: CreditBureau
    score: Optional[int] = Field(None, ge=300, le=850)
    score_model: Optional[ScoreModel] = None
    score_date: Optional[date] = None
    reason_codes: List[str] = Field(default_factory=list)


# ============================================================================
# JazzCreditReport Model - MISMO CreditReport entity ontology schema
# ============================================================================


class JazzCreditReport(BaseModel):
    """
    Jazz CreditReport entity with validation.

    This uses Pydantic's validation and transformation capabilities
    to validate credit report data before sending to the API.
    Follows the MISMO CreditReport entity ontology schema.
    """

    # Required fields
    id: str
    borrower_id: str

    # Report Details
    report_date: Optional[date] = None
    report_type: Optional[ReportType] = None
    credit_provider: Optional[str] = None
    report_reference_number: Optional[str] = None

    # Credit Scores
    scores: List[CreditScore] = Field(default_factory=list)
    representative_score: Optional[int] = Field(None, ge=300, le=850)
    representative_score_model: Optional[str] = None

    # Trade Line Summary
    total_trade_lines: Optional[int] = Field(None, ge=0)
    open_trade_lines: Optional[int] = Field(None, ge=0)
    total_accounts_with_late: Optional[int] = Field(None, ge=0)
    total_collections: Optional[int] = Field(None, ge=0)
    total_public_records: Optional[int] = Field(None, ge=0)
    total_inquiries_last_12_months: Optional[int] = Field(None, ge=0)
    oldest_trade_line_date: Optional[date] = None

    # Provenance (optional, typically added by system)
    provenance: Optional[dict] = None


class CreditReportPayloadBuilder:
    """Builds payload for CreditReport entities.

    One CreditReport entity is created per borrower, combining all their credit scores.
    """

    @staticmethod
    def build_all(
        collection_id: str,
        input_data: Dict[str, Any],
        borrower_mappings: List[Dict[str, str]] = None,
        description: str = "Credit report entity",
    ) -> List[Tuple[Dict[str, Any], str, str]]:
        """Build payloads for all credit reports (one per borrower with credit data).

        Args:
            collection_id: The collection ID from project creation.
            input_data: The full input JSON data.
            borrower_mappings: List of dicts with 'internal_id' and 'external_id' keys.
            description: Description for the entities.

        Returns:
            A list of tuples (payload, external_id, borrower_id) for each credit report.
        """
        borrowers = input_data.get("borrowers", [])
        results = []

        for bidx, borrower in enumerate(borrowers):
            # Skip if no credit data
            credit_scores = borrower.get("creditScores", [])
            median_score = borrower.get("medianCreditScore") or {}
            if not credit_scores and not median_score:
                continue

            borrower_internal_id = None
            if borrower_mappings and bidx < len(borrower_mappings) and borrower_mappings[bidx]:
                borrower_internal_id = borrower_mappings[bidx].get("internal_id")
            if not borrower_internal_id:
                borrower_internal_id = generate_stable_uuid(collection_id, "Borrower", bidx)

            try:
                payload, external_id = CreditReportPayloadBuilder.build_single(
                    collection_id=collection_id,
                    borrower=borrower,
                    borrower_internal_id=borrower_internal_id,
                    description=description,
                )
                results.append((payload, external_id, borrower_internal_id))
            except ValidationError as ve:
                logger.error(
                    f"CreditReport validation failed for borrower {borrower_internal_id}: {ve.errors()}"
                )
                raise

        return results

    @staticmethod
    def build_single(
        collection_id: str,
        borrower: Dict[str, Any],
        borrower_internal_id: str,
        description: str = "Credit report entity",
    ) -> Tuple[Dict[str, Any], str]:
        """Build the payload for a single CreditReport entity.

        Args:
            collection_id: The collection ID from project creation.
            borrower: The borrower data with credit info.
            borrower_internal_id: The internal (Knowledge Hub) ID of the borrower.
            description: Description for the entity.

        Returns:
            A tuple of (payload dict, external_credit_id).

        Raises:
            ValidationError: If the credit report data fails Pydantic validation.
        """
        now = get_current_timestamp()
        external_credit_id = generate_stable_uuid(
            collection_id, "CreditReport", borrower_internal_id
        )

        entity_name = f"CreditReport-{borrower_internal_id[:8]}"

        # --- Build scores array using Pydantic models ---
        credit_scores = borrower.get("creditScores", [])
        median_score = borrower.get("medianCreditScore") or {}

        scores = []
        for cs in credit_scores:
            # Extract reason codes from creditScoreFactors
            factors = cs.get("creditScoreFactors", [])
            reason_codes = [f.get("code", "") for f in factors if f.get("code")] if factors else []

            score_entry = CreditScore(
                bureau=cs.get("bureau"),
                score=cs.get("score"),
                score_model=cs.get("creditModelType"),
                reason_codes=reason_codes,
            )
            scores.append(score_entry)

        # --- Get representative score ---
        representative_score = median_score.get("score")
        if not representative_score and scores:
            # Use median of available scores
            score_values = [s.score for s in scores if s.score]
            if score_values:
                score_values.sort()
                mid = len(score_values) // 2
                representative_score = score_values[mid]

        # --- Build and validate JazzCreditReport using Pydantic ---
        validated_credit_report = JazzCreditReport(
            id=external_credit_id,
            borrower_id=borrower_internal_id,
            report_date=borrower.get("creditReportIssuedDate"),
            report_type=borrower.get("creditReportType", "Individual"),
            credit_provider=borrower.get("creditProvider") or None,
            report_reference_number=borrower.get("creditReferenceNumber") or None,
            scores=scores,
            representative_score=representative_score,
            open_trade_lines=borrower.get("openTradelines"),
            total_trade_lines=borrower.get("totalTradelines"),
            total_accounts_with_late=borrower.get("totalAccountsWithLate"),
            total_collections=borrower.get("totalCollections"),
            total_public_records=borrower.get("totalPublicRecords"),
            total_inquiries_last_12_months=borrower.get("totalInquiriesLast12Months"),
            provenance=build_provenance(),
        )

        # Use mode='json' to serialize properly, exclude_none to skip None values
        json_value = validated_credit_report.model_dump(mode="json", exclude_none=True)

        payload = {
            "entity_type": "CreditReport",
            "ontology_id": CREDIT_REPORT_ONTOLOGY_ID,
            "name": entity_name,
            "collection_id": collection_id,
            "json_value": json_value,
            "description": description,
        }

        return payload, external_credit_id
