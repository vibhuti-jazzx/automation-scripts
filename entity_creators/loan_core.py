"""
LoanCore entity payload builder.
"""

import logging
from datetime import date, datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from .base import (
    LOAN_CORE_ONTOLOGY_ID,
    build_provenance,
    get_current_timestamp,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Helper Functions
# ============================================================================


def _round_percentage(v: float | None) -> float | None:
    """Round percentage to 4 decimal places for practical precision."""
    if v is None:
        return None
    return round(v, 4)


# ============================================================================
# Loan Type Literals - Matches MISMO LoanCore ontology schema
# ============================================================================

LoanPurpose = Literal[
    "Purchase",
    "RefinanceRateAndTerm",
    "RefinanceCashOut",
    "RefinanceNoCashOut",
    "RefinanceLimitedCashOut",
    "Construction",
    "ConstructionToPermanent",
    "HomeImprovement",
    "Other",
]

LoanType = Literal[
    "Conventional",
    "Conv",
    "FHA",
    "VA",
    "USDA",
    "Jumbo",
    "SuperJumbo",
    "NonQM",
    "ReverseMortgage",
    "PortfolioProduct",
    "HELOCFirstLien",
    "HELOCSecondLien",
]

AmortizationType = Literal[
    "Fixed",
    "AdjustableRate",
    "GraduatedPaymentMortgage",
    "GrowingEquityMortgage",
    "InterestOnly",
    "Balloon",
    "NegativeAmortization",
    "Step",
    "BuydownTemporary",
    "BuydownPermanent",
]

LienPriority = Literal["First", "Second", "Third", "HELOC"]

LoanStatus = Literal[
    "Application",
    "Processing",
    "Submitted",
    "Underwriting",
    "ConditionalApproval",
    "Approved",
    "ClearToClose",
    "ClosingScheduled",
    "Docs",
    "Funding",
    "Funded",
    "Purchased",
    "Denied",
    "Withdrawn",
    "Cancelled",
    "Suspended",
]

ApplicationStatus = Literal[
    "Initiated",
    "Personal",
    "Property",
    "Employment",
    "Asset",
    "Liabilities",
    "CreditScore",
    "Demographics",
    "Loan",
    "LoanProgram",
    "LoanTerms",
    "Tasks",
]

ARMIndexType = Literal["SOFR", "LIBOR", "CMT", "COFI", "Prime", "MTA", "CD"]

Channel = Literal["Retail", "Wholesale", "Correspondent", "ConsumerDirect", "BrokerDirect"]

QMType = Literal[
    "GeneralQM",
    "SmallCreditorQM",
    "BalloonPaymentQM",
    "SeasonedQM",
    "TemporaryGSEQM",
    "NonQM",
]


# ============================================================================
# JazzLoanCore Model - MISMO LoanCore entity ontology schema
# ============================================================================


class JazzLoanCore(BaseModel):
    """
    Jazz LoanCore entity with validation.

    This uses Pydantic's validation and transformation capabilities
    to validate loan core data before sending to the API.
    Follows the MISMO LoanCore entity ontology schema.
    """

    # Required fields
    id: str
    loan_purpose: LoanPurpose
    loan_type: LoanType
    amortization_type: AmortizationType
    lien_priority: LienPriority
    loan_status: LoanStatus

    # Application Status (Form 1003 progress)
    application_status: ApplicationStatus = "Initiated"

    # Core Identifiers
    loan_number: Optional[str] = Field(None, max_length=50)
    los_loan_id: Optional[str] = None
    vesta_loan_id: Optional[str] = None

    # Loan Amounts
    base_loan_amount_usd: Optional[float] = Field(None, ge=0)
    total_loan_amount_usd: Optional[float] = Field(None, ge=0)

    # Interest Rate & APR
    note_rate_percent: Optional[float] = Field(None, ge=0, le=100)
    apr_percent: Optional[float] = Field(None, ge=0, le=100)

    # Loan Term
    term_months: Optional[int] = Field(None, ge=1, le=480)

    # ARM Details (if applicable)
    arm_index_type: Optional[ARMIndexType] = None
    arm_margin_percent: Optional[float] = Field(None, ge=0, le=100)
    arm_initial_cap_percent: Optional[float] = Field(None, ge=0, le=100)
    arm_periodic_cap_percent: Optional[float] = Field(None, ge=0, le=100)
    arm_lifetime_cap_percent: Optional[float] = Field(None, ge=0, le=100)
    arm_floor_percent: Optional[float] = Field(None, ge=0, le=100)
    arm_first_adjustment_months: Optional[int] = None
    arm_subsequent_adjustment_months: Optional[int] = None

    # LTV Ratios
    ltv_percent: Optional[float] = Field(None, ge=0, le=100)
    cltv_percent: Optional[float] = Field(None, ge=0, le=100)
    hcltv_percent: Optional[float] = Field(None, ge=0, le=100)

    # Key Dates
    application_date: Optional[date] = None
    lock_date: Optional[date] = None
    lock_expiration_date: Optional[date] = None
    expected_closing_date: Optional[date] = None
    actual_closing_date: Optional[date] = None
    first_payment_date: Optional[date] = None
    maturity_date: Optional[date] = None

    # Interest Only & Balloon
    interest_only_period_months: Optional[int] = Field(None, ge=0)
    balloon_payment_due_date: Optional[date] = None
    balloon_payment_amount_usd: Optional[float] = Field(None, ge=0)

    # Prepayment
    prepayment_penalty: Optional[bool] = None
    prepayment_penalty_term_months: Optional[int] = None

    # Escrow
    is_escrow_waived: Optional[bool] = None

    # Channel & Investor
    channel: Optional[Channel] = None
    investor_name: Optional[str] = None
    investor_loan_number: Optional[str] = None

    # Regulatory Flags
    is_hpml: Optional[bool] = None
    is_hoepa: Optional[bool] = None
    is_qm: Optional[bool] = None
    qm_type: Optional[QMType] = None

    # Borrower Financial Summaries
    total_monthly_income_usd: Optional[float] = Field(None, ge=0)
    total_assets_amount_usd: Optional[float] = Field(None, ge=0)
    total_liabilities_monthly_payment_usd: Optional[float] = Field(None, ge=0)

    # Down Payment
    down_payment: Optional[dict] = Field(
        None,
        description="Down payment details: total_amount_usd, percent_of_purchase_price, source_asset_ids",
    )

    # Provenance (optional, typically added by system)
    provenance: Optional[dict] = None

    # Validator to round percentage fields to 4 decimal places
    @field_validator(
        "note_rate_percent",
        "apr_percent",
        "ltv_percent",
        "cltv_percent",
        "hcltv_percent",
        "arm_margin_percent",
        "arm_initial_cap_percent",
        "arm_periodic_cap_percent",
        "arm_lifetime_cap_percent",
        "arm_floor_percent",
        mode="before",
    )
    @classmethod
    def round_percentage(cls, v):
        return _round_percentage(v)


class LoanCorePayloadBuilder:
    """Builds payload for LoanCore entity."""

    # Mapping from input loanAmortizationType to LoanCore amortization_type
    AMORTIZATION_MAP = {
        "FixedRateMortgage": "Fixed",
        "AdjustableRate": "AdjustableRate",
    }

    # Mapping from input lienType to LoanCore lien_priority
    LIEN_MAP = {
        "FirstLien": "First",
        "SecondLien": "Second",
        "ThirdLien": "Third",
    }

    @classmethod
    def build(
        cls,
        loan_id: str,
        collection_id: str,
        input_data: Dict[str, Any],
        description: str = "Loan core entity",
    ) -> Dict[str, Any]:
        """Build the payload for a LoanCore entity.

        Args:
            loan_id: The external loan ID.
            collection_id: The collection ID from project creation.
            input_data: The full input JSON data.
            description: Description for the entity.

        Returns:
            The payload dictionary ready for the entity API.

        Raises:
            ValidationError: If the loan core data fails Pydantic validation.
        """
        now = get_current_timestamp()
        loan = input_data.get("loan") or {}
        loan_product = loan.get("loanProduct") or {}
        borrowers = input_data.get("borrowers") or []
        subject_property = input_data.get("subject_property") or {}

        # --- Compute aggregated values ---
        total_monthly_income = sum(
            b.get("totalNetIncome", 0) or 0 for b in borrowers
        )

        total_assets = 0.0
        for b in borrowers:
            for asset in b.get("assets", []):
                total_assets += asset.get("currentBalance", 0) or 0

        total_liabilities_monthly = 0.0
        for b in borrowers:
            for liability in b.get("liabilities", []):
                total_liabilities_monthly += liability.get("monthlyPaymentAmount",
                                                           liability.get("monthlyPayment", 0)) or 0

        # --- Get LTV values from input or use defaults ---
        ltv = loan.get("ltvPercent", loan.get("ltv"))
        cltv = loan.get("cltvPercent", loan.get("cltv"))
        hcltv = loan.get("hcltvPercent", loan.get("hcltv"))

        # --- Down payment ---
        down_payment_input = loan.get("downPayment") or {}
        down_payment = {
            "source_asset_ids": [],
            "total_amount_usd": down_payment_input.get("amount", 0),
            "percent_of_purchase_price": down_payment_input.get("percentage", 0),
        }

        # --- Amortization & lien mapping ---
        amortization_type = cls.AMORTIZATION_MAP.get(
            loan_product.get("loanAmortizationType", ""),
            loan_product.get("loanAmortizationType", "Fixed"),
        )
        lien_priority = cls.LIEN_MAP.get(
            loan_product.get("lienType", ""),
            loan_product.get("lienType", "First"),
        )

        # --- Application date (from createdAt) ---
        created_at = loan.get("createdAt", now)
        application_date = None
        if created_at:
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                application_date = dt.strftime("%Y-%m-%d")
            except (ValueError, AttributeError):
                application_date = created_at[:10] if len(created_at) >= 10 else None

        # --- Expected closing date ---
        closing_date = loan.get("closingDate", None)

        # --- Escrow ---
        is_escrow_waived = not loan.get("borrowersWillPayTIFromEscrowIndicator", True)

        # --- Build and validate JazzLoanCore using Pydantic ---
        try:
            validated_loan_core = JazzLoanCore(
                id=loan_id,
                loan_purpose=loan.get("loanPurpose", "Purchase"),
                loan_type=loan_product.get("mortgageType", "Conventional"),
                amortization_type=amortization_type,
                lien_priority=lien_priority,
                loan_status=loan.get("currentLoanStage", "Application"),
                application_status="Initiated",
                loan_number=loan_id,
                vesta_loan_id=None,
                base_loan_amount_usd=float(loan.get("loanAmount", 0)) if loan.get("loanAmount") else None,
                total_loan_amount_usd=float(loan.get("loanAmount", 0)) if loan.get("loanAmount") else None,
                note_rate_percent=loan_product.get("pricingEngineNoteRate"),
                term_months=loan_product.get("loanTermMonthsCount"),
                ltv_percent=ltv,
                cltv_percent=cltv,
                hcltv_percent=hcltv,
                application_date=application_date,
                expected_closing_date=closing_date,
                is_escrow_waived=is_escrow_waived,
                down_payment=down_payment,
                total_monthly_income_usd=round(total_monthly_income, 2) if total_monthly_income else None,
                total_assets_amount_usd=round(total_assets, 2) if total_assets else None,
                total_liabilities_monthly_payment_usd=round(total_liabilities_monthly, 2) if total_liabilities_monthly else None,
                provenance=build_provenance(created_at),
            )

            # Use mode='json' to serialize properly, exclude_none to skip None values
            json_value = validated_loan_core.model_dump(mode="json", exclude_none=True)

        except ValidationError as ve:
            logger.error(f"LoanCore validation failed: {ve.errors()}")
            raise

        return {
            "entity_type": "LoanCore",
            "ontology_id": LOAN_CORE_ONTOLOGY_ID,
            "name": "LoanCore",
            "collection_id": collection_id,
            "json_value": json_value,
            "description": description,
        }
