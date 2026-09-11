"""
LoanDetails entity payload builder.

The LoanDetails entity is a high-level summary view of the loan that aggregates
key information from the loan, borrowers, income, and property data into a single
dashboard-friendly entity.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from .base import (
    LOAN_DETAILS_ONTOLOGY_ID,
    get_current_timestamp,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Borrower Type Literal
# ============================================================================

BorrowerType = Literal["borrower", "coborrower"]


# ============================================================================
# Nested Models - Summary Section
# ============================================================================


class LoanSummary(BaseModel):
    """Summary section of loan details."""

    loanId: str
    status: str
    borrowerName: str
    loanAmount: float = Field(..., ge=0)
    loanType: Optional[str] = None
    closingDate: Optional[str] = Field(None, description="Date in YYYY-MM-DD format")
    propertyValueTop: Optional[float] = Field(None, ge=0)


# ============================================================================
# Nested Models - Loan Overview Section
# ============================================================================


class DtiRatio(BaseModel):
    """DTI ratio breakdown."""

    frontEnd: float = Field(..., ge=0)
    backEnd: float = Field(..., ge=0)
    assessment: Optional[str] = None


class LtvRatio(BaseModel):
    """LTV ratio details."""

    value: float = Field(..., ge=0)
    assessment: Optional[str] = None


class InterestRateApr(BaseModel):
    """Interest rate and APR details."""

    interestRate: float = Field(..., ge=0)
    apr: float = Field(..., ge=0)


class LoanOverview(BaseModel):
    """Loan overview section of loan details."""

    loanAmount: float = Field(..., ge=0)
    interestRateApr: InterestRateApr
    applicationDate: Optional[str] = Field(None, description="Date in YYYY-MM-DD format")
    loanProgram: str
    principalAndInterestPayment: float = Field(..., ge=0)
    loanOfficer: str
    dtiRatio: DtiRatio
    ltvRatio: LtvRatio


# ============================================================================
# Nested Models - Property Information Section
# ============================================================================


class PropertyInformation(BaseModel):
    """Property information section of loan details."""

    propertyAddress: str
    propertyType: str
    purchasePrice: float = Field(..., ge=0)
    occupancy: str
    appraisalValue: Optional[str] = None


# ============================================================================
# Nested Models - Borrower Details Section
# ============================================================================


class BureauBreakdown(BaseModel):
    """Credit bureau score breakdown."""

    equifax: Optional[int] = Field(None, ge=300, le=850)
    experian: Optional[int] = Field(None, ge=300, le=850)
    transUnion: Optional[int] = Field(None, ge=300, le=850)


class CreditScore(BaseModel):
    """Credit score details."""

    representative: Optional[int] = Field(None, ge=300, le=850)
    bureauBreakdown: Optional[BureauBreakdown] = None


class BorrowerProfile(BaseModel):
    """Borrower profile details."""

    borrowerName: str
    creditScore: Optional[CreditScore] = None
    currentAddress: Optional[str] = None
    dateOfBirth: Optional[str] = Field(None, description="Date in YYYY-MM-DD format")
    emailAddress: Optional[str] = None
    maritalStatus: Optional[str] = None
    phoneNumber: Optional[str] = None
    socialSecurityNumberMasked: Optional[str] = None


class IncomeChartEntry(BaseModel):
    """Income chart entry for a specific year."""

    year: int
    income: float = Field(..., ge=0)


class EmploymentAndIncome(BaseModel):
    """Employment and income details."""

    totalMonthlyQualifyingIncome: float = Field(..., ge=0)
    employmentType: str
    employer: str
    yearsAtJob: Optional[float] = Field(None, ge=0)
    employmentStartDate: Optional[str] = Field(None, description="Date in YYYY-MM-DD format")
    totalMonthlyQualifyingIncomeChart: Optional[List[IncomeChartEntry]] = None


class BorrowerDetail(BaseModel):
    """Individual borrower details."""

    borrowerType: BorrowerType
    borrowerProfile: BorrowerProfile
    employmentAndIncome: List[EmploymentAndIncome] = Field(default_factory=list)


# ============================================================================
# JazzLoanDetails Model - LoanDetails entity ontology schema
# ============================================================================


class JazzLoanDetails(BaseModel):
    """
    Jazz LoanDetails entity for loan details page.

    This schema represents the loan details page sections and fields,
    providing a high-level summary view of the loan that aggregates
    key information from the loan, borrowers, income, and property data.
    """

    summary: LoanSummary
    loanOverview: LoanOverview
    propertyInformation: PropertyInformation
    borrowerDetails: List[BorrowerDetail] = Field(..., min_length=1)


class LoanDetailsPayloadBuilder:
    """Builds payload for LoanDetails entity."""

    @staticmethod
    def build(
        loan_id: str,
        collection_id: str,
        input_data: Dict[str, Any],
        description: str = "Loan details entity",
    ) -> Dict[str, Any]:
        """Build the payload for a LoanDetails entity.

        Args:
            loan_id: The external loan ID.
            collection_id: The collection ID from project creation.
            input_data: The full input JSON data.
            description: Description for the entity.

        Returns:
            The payload dictionary ready for the entity API.

        Raises:
            ValidationError: If the loan details data fails Pydantic validation.
        """
        now = get_current_timestamp()
        loan = input_data.get("loan") or {}
        loan_product = loan.get("loanProduct") or {}
        borrowers = input_data.get("borrowers") or []
        subject_property = input_data.get("subject_property") or {}

        # --- Identify primary borrower ---
        primary_borrower = borrowers[0] if borrowers else {}
        primary_first = primary_borrower.get("firstName", "")
        primary_middle = primary_borrower.get("middleName", "")
        primary_last = primary_borrower.get("lastName", "")
        primary_full_name = " ".join(p for p in [primary_first, primary_middle, primary_last] if p)
        if not primary_full_name:
            primary_full_name = "Unknown"

        # --- Loan amounts ---
        loan_amount = float(loan.get("loanAmount", 0) or 0)
        property_value = float(
            subject_property.get("estimatedValueAmount",
                                 subject_property.get("estimatedValue",
                                                      subject_property.get("purchasePrice", 0))) or 0
        )

        # --- LTV ---
        ltv = round((loan_amount / property_value) * 100, 1) if property_value > 0 else 0

        # --- Closing date ---
        closing_date = loan.get("closingDate", None)

        # --- Loan status ---
        loan_status = loan.get("currentLoanStage", "Application Received")

        # --- Application date ---
        created_at = loan.get("createdAt", now)
        application_date = None
        if created_at:
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                application_date = dt.strftime("%Y-%m-%d")
            except (ValueError, AttributeError):
                application_date = created_at[:10] if len(created_at) >= 10 else None

        # --- Interest rate / APR ---
        note_rate = loan_product.get("pricingEngineNoteRate", 0)
        apr = loan_product.get("apr", note_rate)

        # --- Loan program description ---
        term_months = loan_product.get("loanTermMonthsCount", 0)
        mortgage_type = loan_product.get("mortgageType", "")
        amortization = loan_product.get("loanAmortizationType", "")
        rate_label = "Fixed Rate" if amortization == "FixedRateMortgage" else (
            "Adjustable Rate" if amortization == "AdjustableRate" else amortization
        )
        term_years = int(term_months / 12) if term_months else 0
        loan_program = f"{term_years}-Year {mortgage_type} - {rate_label}" if term_years else mortgage_type

        # --- Compute total monthly liabilities ---
        total_monthly_liabilities = 0.0
        for b in borrowers:
            for liability in b.get("liabilities", []):
                total_monthly_liabilities += liability.get("monthlyPaymentAmount",
                                                           liability.get("monthlyPayment", 0)) or 0

        # --- Compute total monthly income ---
        total_monthly_income = sum(
            b.get("totalNetIncome", 0) or 0 for b in borrowers
        )

        # --- DTI ratios ---
        front_end_dti = 0
        back_end_dti = 0
        dti_assessment = "Unknown"
        if total_monthly_income > 0:
            back_end_dti = round((total_monthly_liabilities / total_monthly_income) * 100, 1)
            if back_end_dti <= 36:
                dti_assessment = "Good"
            elif back_end_dti <= 43:
                dti_assessment = "Acceptable"
            elif back_end_dti <= 50:
                dti_assessment = "High"
            else:
                dti_assessment = "Very High"

        # --- LTV assessment ---
        ltv_assessment = "Unknown"
        if ltv > 0:
            if ltv <= 80:
                ltv_assessment = "Good"
            elif ltv <= 90:
                ltv_assessment = "Acceptable"
            elif ltv <= 95:
                ltv_assessment = "High"
            else:
                ltv_assessment = "Very High"

        # --- Property information ---
        prop_type = subject_property.get("propertyType", "Unknown")
        occupancy = subject_property.get("intendedUsageType",
                                          subject_property.get("occupancyType", "PrimaryResidence"))
        purchase_price = float(subject_property.get("purchasePrice",
                                                     subject_property.get("estimatedValueAmount", 0)) or 0)
        appraisal_value = subject_property.get("appraisedValue",
                                                subject_property.get("appraisalValue", None))
        if appraisal_value is not None:
            appraisal_value = str(appraisal_value)

        # --- Property address ---
        prop_addr = subject_property.get("address") or {}
        prop_addr_parts = [
            prop_addr.get("line", prop_addr.get("fullStreetAddress", "")),
            prop_addr.get("city", ""),
            prop_addr.get("stateCode", prop_addr.get("state", "")),
            prop_addr.get("zipCode", prop_addr.get("zip", "")),
        ]
        property_address = ", ".join(p for p in prop_addr_parts if p)

        # --- Borrower details ---
        borrower_details = LoanDetailsPayloadBuilder._build_borrower_details(borrowers)

        # --- Build and validate JazzLoanDetails using Pydantic ---
        entity_name = f"LoanDetails-{loan_id}"

        try:
            validated_loan_details = JazzLoanDetails(
                summary=LoanSummary(
                    loanId=loan_id,
                    status=loan_status,
                    loanType=loan_product.get("mortgageType"),
                    loanAmount=loan_amount,
                    closingDate=closing_date,
                    borrowerName=primary_full_name,
                    propertyValueTop=property_value,
                ),
                loanOverview=LoanOverview(
                    dtiRatio=DtiRatio(
                        backEnd=back_end_dti,
                        frontEnd=front_end_dti,
                        assessment=dti_assessment,
                    ),
                    ltvRatio=LtvRatio(
                        value=ltv,
                        assessment=ltv_assessment,
                    ),
                    loanAmount=loan_amount,
                    loanOfficer=loan.get("loanOfficer", "Not Assigned"),
                    loanProgram=loan_program if loan_program else "Unknown",
                    applicationDate=application_date,
                    interestRateApr=InterestRateApr(
                        apr=float(apr) if apr else 0,
                        interestRate=float(note_rate) if note_rate else 0,
                    ),
                    principalAndInterestPayment=float(loan.get("principalAndInterestPayment", 0) or 0),
                ),
                borrowerDetails=borrower_details,
                propertyInformation=PropertyInformation(
                    occupancy=occupancy,
                    propertyType=prop_type,
                    purchasePrice=purchase_price,
                    appraisalValue=appraisal_value,
                    propertyAddress=property_address if property_address else "Unknown",
                ),
            )

            # Use mode='json' to serialize properly, exclude_none to skip None values
            json_value = validated_loan_details.model_dump(mode="json", exclude_none=True)

        except ValidationError as ve:
            logger.error(f"LoanDetails validation failed: {ve.errors()}")
            raise

        return {
            "entity_type": "LoanDetails",
            "ontology_id": LOAN_DETAILS_ONTOLOGY_ID,
            "name": entity_name,
            "collection_id": collection_id,
            "json_value": json_value,
            "description": description,
        }

    @staticmethod
    def _build_borrower_details(borrowers: List[Dict[str, Any]]) -> List[BorrowerDetail]:
        """Build borrower details array for LoanDetails entity."""
        borrower_details = []

        for idx, b in enumerate(borrowers):
            b_type: BorrowerType = "borrower" if idx == 0 else "coborrower"
            first = b.get("firstName", "")
            middle = b.get("middleName", "")
            last = b.get("lastName", "")
            full_name = " ".join(p for p in [first, middle, last] if p)
            if not full_name:
                full_name = "Unknown"

            # Credit scores
            credit_scores = b.get("creditScores", [])
            median_score_obj = b.get("medianCreditScore") or {}
            representative = median_score_obj.get("score") or None
            equifax = None
            experian = None
            transunion = None
            for cs in credit_scores:
                bureau = cs.get("bureau", "")
                score = cs.get("score")
                if score:
                    if bureau == "Equifax":
                        equifax = score
                    elif bureau == "Experian":
                        experian = score
                    elif bureau == "TransUnion":
                        transunion = score

            # Current address
            addr = b.get("currentAddress") or {}
            addr_parts = [
                addr.get("line", addr.get("fullStreetAddress", addr.get("street", ""))),
                addr.get("city", ""),
                addr.get("stateCode", addr.get("state", "")),
            ]
            current_address_str = ", ".join(p for p in addr_parts if p)

            # Phone
            phone = b.get("cellPhoneNumber", "")
            if not phone:
                for pn in b.get("phoneNumbers", []):
                    if pn.get("type") == "Mobile":
                        phone = pn.get("number", "")
                        break
            if not phone:
                phone = b.get("homePhoneNumber", "")

            # Employment & income
            employment_and_income = LoanDetailsPayloadBuilder._build_employment_income(b)

            # Build Pydantic models
            bureau_breakdown = BureauBreakdown(
                equifax=equifax,
                experian=experian,
                transUnion=transunion,
            ) if any([equifax, experian, transunion]) else None

            credit_score = CreditScore(
                representative=representative,
                bureauBreakdown=bureau_breakdown,
            ) if representative or bureau_breakdown else None

            borrower_profile = BorrowerProfile(
                borrowerName=full_name,
                creditScore=credit_score,
                dateOfBirth=b.get("dateOfBirth"),
                phoneNumber=phone if phone else "999-999-9999",
                emailAddress=b.get("emailAddress", b.get("email")) or None,
                maritalStatus=b.get("maritalStatus"),
                currentAddress=current_address_str if current_address_str else None,
                socialSecurityNumberMasked=b.get("ssnMasked", "XXX-XX-0000"),
            )

            borrower_details.append(BorrowerDetail(
                borrowerType=b_type,
                borrowerProfile=borrower_profile,
                employmentAndIncome=employment_and_income,
            ))

        return borrower_details

    @staticmethod
    def _build_employment_income(borrower: Dict[str, Any]) -> List[EmploymentAndIncome]:
        """Build employment and income array for a borrower."""
        employment_and_income = []

        for income in borrower.get("incomes", []):
            employer_data = income.get("employer") or {}
            employer_name = employer_data.get("name", "")
            employment_type = employer_data.get("employmentClassificationType", "Employed")
            start_date = employer_data.get("startDate", "")

            # Calculate years at job
            years_at_job = 0.0
            if start_date:
                try:
                    # Parse the start date - handle both naive and aware formats
                    if "T" in start_date or "+" in start_date or start_date.endswith("Z"):
                        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                    else:
                        # Date-only format (e.g., "2008-03-03") - parse as naive then make aware
                        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

                    now_dt = datetime.now(timezone.utc)
                    years_at_job = round((now_dt - start_dt).days / 365.25, 2)
                except (ValueError, AttributeError):
                    years_at_job = employer_data.get("numberOfMonthsInThisLineOfWork", 0) / 12.0

            # Monthly qualifying income
            monthly_qual = income.get("totalCalculatedQualifiedMonthlyIncome", 0)
            if not monthly_qual:
                qual_base = income.get("qualifiedEmploymentBaseIncome") or {}
                if qual_base.get("amount"):
                    monthly_qual = qual_base["amount"]
            if not monthly_qual:
                stated_base = income.get("statedEmploymentBaseIncome") or {}
                if stated_base.get("amount"):
                    monthly_qual = stated_base["amount"]

            annual_income = float(monthly_qual or 0) * 12

            # Build income chart for the last 5 years including current year
            income_chart = []
            current_year = datetime.now(timezone.utc).year
            for yr in range(current_year - 4, current_year + 1):
                income_chart.append(IncomeChartEntry(
                    year=yr,
                    income=annual_income,
                ))

            employment_and_income.append(EmploymentAndIncome(
                employer=employer_name if employer_name else "Unknown",
                yearsAtJob=round(years_at_job, 2),
                employmentType=employment_type if employment_type else "Employed",
                employmentStartDate=start_date if start_date else None,
                totalMonthlyQualifyingIncome=float(monthly_qual or 0),
                totalMonthlyQualifyingIncomeChart=income_chart,
            ))

        return employment_and_income
