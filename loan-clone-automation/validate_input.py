#!/usr/bin/env python3
"""
Input Validation Script for Entity Creators

This script validates input.json against all entity creator requirements
and lists all missing or incorrect fields without stopping on first error.

Usage:
    python validate_input.py --input input.json
    python validate_input.py --input input.json --verbose
"""

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, get_args


# =============================================================================
# Validation Error Types
# =============================================================================

class ErrorSeverity(Enum):
    ERROR = "ERROR"      # Will cause entity creation to fail
    WARNING = "WARNING"  # May cause issues but won't block creation
    INFO = "INFO"        # Informational - field is optional but recommended


@dataclass
class ValidationError:
    """Represents a single validation error."""
    entity_type: str
    field_path: str
    message: str
    severity: ErrorSeverity = ErrorSeverity.ERROR
    expected: Optional[str] = None
    actual: Optional[Any] = None

    def __str__(self) -> str:
        base = f"[{self.severity.value}] {self.entity_type} -> {self.field_path}: {self.message}"
        if self.expected:
            base += f" (expected: {self.expected})"
        if self.actual is not None:
            actual_str = str(self.actual)[:50] + "..." if len(str(self.actual)) > 50 else str(self.actual)
            base += f" (got: {actual_str})"
        return base


@dataclass
class ValidationResult:
    """Holds all validation results."""
    errors: List[ValidationError] = field(default_factory=list)

    def add_error(
        self,
        entity_type: str,
        field_path: str,
        message: str,
        severity: ErrorSeverity = ErrorSeverity.ERROR,
        expected: Optional[str] = None,
        actual: Optional[Any] = None,
    ):
        self.errors.append(ValidationError(
            entity_type=entity_type,
            field_path=field_path,
            message=message,
            severity=severity,
            expected=expected,
            actual=actual,
        ))

    @property
    def has_errors(self) -> bool:
        return any(e.severity == ErrorSeverity.ERROR for e in self.errors)

    @property
    def error_count(self) -> int:
        return sum(1 for e in self.errors if e.severity == ErrorSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for e in self.errors if e.severity == ErrorSeverity.WARNING)

    @property
    def info_count(self) -> int:
        return sum(1 for e in self.errors if e.severity == ErrorSeverity.INFO)


# =============================================================================
# Allowed Value Definitions (from entity creators)
# =============================================================================

# LoanCore allowed values
LOAN_PURPOSES = [
    "Purchase", "RefinanceRateAndTerm", "RefinanceCashOut", "RefinanceNoCashOut",
    "RefinanceLimitedCashOut", "Construction", "ConstructionToPermanent",
    "HomeImprovement", "Other"
]

LOAN_TYPES = [
    "Conventional", "Conv", "FHA", "VA", "USDA", "Jumbo", "SuperJumbo", "NonQM",
    "ReverseMortgage", "PortfolioProduct", "HELOCFirstLien", "HELOCSecondLien"
]

AMORTIZATION_TYPES = [
    "FixedRateMortgage", "AdjustableRate", "GraduatedPaymentMortgage",
    "GrowingEquityMortgage", "InterestOnly", "Balloon", "NegativeAmortization",
    "Step", "BuydownTemporary", "BuydownPermanent"
]

LIEN_TYPES = ["FirstLien", "SecondLien", "ThirdLien", "HELOC"]

LOAN_STATUSES = [
    "Application", "Processing", "Submitted", "Underwriting", "ConditionalApproval",
    "Approved", "ClearToClose", "ClosingScheduled", "Docs", "Funding", "Funded",
    "Purchased", "Denied", "Withdrawn", "Cancelled", "Suspended"
]

ARM_INDEX_TYPES = ["SOFR", "LIBOR", "CMT", "COFI", "Prime", "MTA", "CD"]

# Borrower allowed values
CITIZENSHIP_STATUSES = ["USCitizen", "PermanentResidentAlien", "NonPermanentResidentAlien"]

MARITAL_STATUSES = ["Married", "Separated", "Unmarried"]

NAME_SUFFIXES = ["Jr", "Sr", "II", "III", "IV"]

HMDA_ETHNICITIES = [
    "HispanicOrLatino", "NotHispanicOrLatino", "InformationNotProvided", "NotApplicable"
]

HMDA_RACES = [
    "AmericanIndianOrAlaskaNative", "Asian", "BlackOrAfricanAmerican",
    "NativeHawaiianOrOtherPacificIslander", "White", "InformationNotProvided", "NotApplicable"
]

HMDA_SEXES = ["Female", "Male", "InformationNotProvided", "NotApplicable"]

# Asset allowed values
ASSET_TYPES = [
    "CheckingAccount", "SavingsAccount", "MoneyMarket", "CertificateOfDeposit",
    "MutualFund", "Stock", "Bond", "RetirementAccount", "IRA", "401k", "403b",
    "PensionFund", "TrustFund", "LifeInsuranceCashValue", "StockOptions",
    "BridgeLoanProceeds", "IndividualDevelopmentAccount", "CashOnHand", "GiftFunds",
    "Grant", "RealEstateEquity", "SecuredBorrowedFunds", "UnsecuredBorrowedFunds",
    "SaleOfChattel", "TradeEquity", "SweatEquity", "CashDepositOnSalesContract",
    "RelocationFunds", "EmployerAssistedHousing", "LeasePurchaseFund", "LotEquity",
    "RentWithOptionToPurchase", "OtherLiquidAssets", "OtherNonLiquidAssets",
    "ProceedsFromSaleOfHome", "ProceedsFromSecuredLoan", "ProceedsFromUnsecuredLoan",
    "EarnestMoneyDeposit"
]

# Income allowed values
INCOME_TYPES = [
    "Base", "Overtime", "Bonus", "Commission", "MilitaryBasePay", "MilitaryRationsAllowance",
    "MilitaryFlightPay", "MilitaryHazardPay", "MilitaryClothesAllowance",
    "MilitaryQuartersAllowance", "MilitaryPropPay", "MilitaryOverseasPay",
    "MilitaryCombatPay", "MilitaryVariableHousingAllowance", "SelfEmployment",
    "SocialSecurity", "Pension", "Retirement", "Disability", "ChildSupport", "Alimony",
    "RentalIncome", "InterestDividends", "NotesReceivable", "Trust", "OtherIncome",
    "AutomobileAllowance", "BoarderIncome", "CapitalGains", "EmploymentRelatedAssets",
    "FosterCare", "HousingAllowance", "MortgageCreditCertificate", "MortgageDifferential",
    "PublicAssistance", "RoyaltyPayment", "SeasonalIncome", "SecondaryEmployment",
    "TemporaryLeave", "TipIncome", "UnemploymentBenefits", "VABenefits",
    "AccessoryUnitIncome", "Employment"
]

INCOME_FREQUENCIES = [
    "Weekly", "BiWeekly", "SemiMonthly", "Monthly", "Quarterly", "SemiAnnually", "Annually"
]

DOCUMENTATION_LEVELS = [
    "FullDocumentation", "ReducedDocumentation", "NoDocumentation", "Stated"
]

VERIFICATION_STATUSES = [
    "NotVerified", "VerbalVOE", "WrittenVOE", "PaystubVerified",
    "TaxReturnVerified", "BankStatementVerified"
]

# Liability allowed values
LIABILITY_TYPES = [
    "Mortgage", "HELOC", "Installment", "Revolving", "OpenThirtyDay", "LeasePayment",
    "ChildSupport", "Alimony", "SeparateMaintenanceExpense", "JobRelatedExpense",
    "Other", "CollectionsJudgments", "DeferredStudentLoan", "GovernmentStudentLoan",
    "Taxes", "MedicalDebt", "AutoLoan", "StudentLoan", "PersonalLoan"
]

# Subject Property allowed values
# Note: "SingleFamilyResidence" is NOT valid - use "SingleFamily" instead
PROPERTY_TYPES = [
    "SingleFamily", "Condominium", "Townhouse", "Cooperative", "TwoToFourUnit",
    "ManufacturedSingleWide", "ManufacturedDoubleWide", "ManufacturedMultiWide",
    "PUD", "Modular", "MixedUse", "DetachedCondominium", "HighRiseCondominium"
]

OCCUPANCY_TYPES = ["PrimaryResidence", "SecondHome", "Investment"]

CONSTRUCTION_TYPES = ["Existing", "NewConstruction", "ConstructionToPermanent", "Proposed"]

MANUFACTURED_WIDTH_TYPES = ["SingleWide", "DoubleWide", "MultiWide"]

CONDO_PROJECT_CLASSIFICATIONS = [
    "Established", "NewProject", "TwoToFourUnitProject", "DetachedCondo",
    "Manufactured", "NonWarrantable"
]

TITLE_MANNER_HELD = [
    "JointTenants", "TenantsInCommon", "CommunityProperty", "SoleOwnership",
    "Trust", "LLC", "Corporation", "Partnership", "LifeEstate"
]

# Employment allowed values
EMPLOYMENT_TYPES = ["Current", "Previous", "Secondary"]

EMPLOYMENT_STATUSES = [
    "Employed", "SelfEmployed", "Retired", "NotEmployed", "Military", "IndependentContractor"
]

EMPLOYMENT_CLASSIFICATION_TYPES = ["Primary", "Current", "Previous", "Secondary"]

# Credit Report allowed values
CREDIT_BUREAUS = ["Equifax", "Experian", "TransUnion"]

SCORE_MODELS = [
    "FICO8", "FICO9", "FICO10", "FICO10T", "FICOAuto", "FICOBankcard",
    "VantageScore3", "VantageScore4", "ClassicFICO",
    # Additional vendor-specific names found in real data
    "FICO Classic v5", "FICO Classic 04", "FICO Classic 98",
    "FICO Score 8", "FICO Score 9", "FICO Score 10",
]

# Note: Only these 4 values are accepted by the schema
# "Merge Credit Report" and "MergeReport" are NOT valid - use "TriMerge" instead
REPORT_TYPES = [
    "Individual", "Joint", "TriMerge", "SingleBureau",
]

# US State codes
US_STATE_CODES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "PR", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "VI", "WA", "WV", "WI",
    "WY", "GU", "AS", "MP"
]


# =============================================================================
# Validation Helper Functions
# =============================================================================

def validate_required_field(
    result: ValidationResult,
    data: Dict[str, Any],
    field_name: str,
    entity_type: str,
    path_prefix: str = "",
) -> bool:
    """Check if a required field exists and is not None or empty."""
    full_path = f"{path_prefix}.{field_name}" if path_prefix else field_name
    value = data.get(field_name)

    if value is None:
        result.add_error(entity_type, full_path, "Required field is missing")
        return False

    if isinstance(value, str) and not value.strip():
        result.add_error(entity_type, full_path, "Required field is empty")
        return False

    return True


def validate_enum_field(
    result: ValidationResult,
    data: Dict[str, Any],
    field_name: str,
    allowed_values: List[str],
    entity_type: str,
    path_prefix: str = "",
    required: bool = False,
    allow_empty_string: bool = True,
) -> bool:
    """Validate that a field's value is in the allowed set."""
    full_path = f"{path_prefix}.{field_name}" if path_prefix else field_name
    value = data.get(field_name)

    if value is None:
        if required:
            result.add_error(entity_type, full_path, "Required field is missing", expected=", ".join(allowed_values))
            return False
        return True

    # Allow empty strings as "not provided" unless explicitly required
    if allow_empty_string and value == "":
        return True

    if value not in allowed_values:
        result.add_error(
            entity_type, full_path,
            f"Invalid value",
            expected=f"one of [{', '.join(allowed_values[:5])}{'...' if len(allowed_values) > 5 else ''}]",
            actual=value,
        )
        return False

    return True


def validate_enum_array(
    result: ValidationResult,
    data: Dict[str, Any],
    field_name: str,
    allowed_values: List[str],
    entity_type: str,
    path_prefix: str = "",
    allow_empty_strings: bool = True,
) -> bool:
    """Validate that all values in an array field are in the allowed set."""
    full_path = f"{path_prefix}.{field_name}" if path_prefix else field_name
    value = data.get(field_name)

    if value is None:
        return True

    if not isinstance(value, list):
        result.add_error(entity_type, full_path, "Expected an array", actual=type(value).__name__)
        return False

    valid = True
    for i, item in enumerate(value):
        # Skip empty strings if allowed (common in real data)
        if allow_empty_strings and item == "":
            continue
        if item not in allowed_values:
            result.add_error(
                entity_type, f"{full_path}[{i}]",
                f"Invalid value in array",
                expected=f"one of [{', '.join(allowed_values[:5])}...]",
                actual=item,
            )
            valid = False

    return valid


def validate_numeric_field(
    result: ValidationResult,
    data: Dict[str, Any],
    field_name: str,
    entity_type: str,
    path_prefix: str = "",
    min_val: Optional[float] = None,
    max_val: Optional[float] = None,
    required: bool = False,
) -> bool:
    """Validate that a numeric field has a valid value within range."""
    full_path = f"{path_prefix}.{field_name}" if path_prefix else field_name
    value = data.get(field_name)

    if value is None:
        if required:
            result.add_error(entity_type, full_path, "Required field is missing")
            return False
        return True

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        result.add_error(entity_type, full_path, "Expected a number", actual=type(value).__name__)
        return False

    if not math.isfinite(value):
        result.add_error(entity_type, full_path, "Expected a finite number", actual=value)
        return False

    if min_val is not None and value < min_val:
        result.add_error(entity_type, full_path, f"Value below minimum", expected=f">= {min_val}", actual=value)
        return False

    if max_val is not None and value > max_val:
        result.add_error(entity_type, full_path, f"Value above maximum", expected=f"<= {max_val}", actual=value)
        return False

    return True


def validate_integer_field(
    result: ValidationResult,
    data: Dict[str, Any],
    field_name: str,
    entity_type: str,
    path_prefix: str = "",
    min_val: Optional[int] = None,
    max_val: Optional[int] = None,
    required: bool = False,
) -> bool:
    """Validate that an integer field has a valid value within range."""
    full_path = f"{path_prefix}.{field_name}" if path_prefix else field_name
    value = data.get(field_name)

    if value is None:
        if required:
            result.add_error(entity_type, full_path, "Required field is missing")
            return False
        return True

    if not isinstance(value, int) or isinstance(value, bool):
        result.add_error(entity_type, full_path, "Expected an integer", actual=type(value).__name__)
        return False

    if min_val is not None and value < min_val:
        result.add_error(entity_type, full_path, f"Value below minimum", expected=f">= {min_val}", actual=value)
        return False

    if max_val is not None and value > max_val:
        result.add_error(entity_type, full_path, f"Value above maximum", expected=f"<= {max_val}", actual=value)
        return False

    return True


def validate_string_field(
    result: ValidationResult,
    data: Dict[str, Any],
    field_name: str,
    entity_type: str,
    path_prefix: str = "",
    max_length: Optional[int] = None,
    required: bool = False,
    pattern: Optional[str] = None,
    pattern_desc: Optional[str] = None,
) -> bool:
    """Validate a string field."""
    full_path = f"{path_prefix}.{field_name}" if path_prefix else field_name
    value = data.get(field_name)

    if value is None:
        if required:
            result.add_error(entity_type, full_path, "Required field is missing")
            return False
        return True

    if not isinstance(value, str):
        result.add_error(entity_type, full_path, "Expected a string", actual=type(value).__name__)
        return False

    if max_length and len(value) > max_length:
        result.add_error(entity_type, full_path, f"String too long", expected=f"max {max_length} chars", actual=f"{len(value)} chars")
        return False

    if pattern and not re.match(pattern, value):
        result.add_error(entity_type, full_path, pattern_desc or f"Invalid format", expected=pattern, actual=value)
        return False

    return True


def validate_date_field(
    result: ValidationResult,
    data: Dict[str, Any],
    field_name: str,
    entity_type: str,
    path_prefix: str = "",
    required: bool = False,
) -> bool:
    """Validate a date field (YYYY-MM-DD format)."""
    full_path = f"{path_prefix}.{field_name}" if path_prefix else field_name
    value = data.get(field_name)

    if value is None or value == "":
        if required:
            result.add_error(entity_type, full_path, "Required field is missing")
            return False
        return True

    if not isinstance(value, str):
        result.add_error(entity_type, full_path, "Expected a date string", actual=type(value).__name__)
        return False

    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        result.add_error(entity_type, full_path, "Invalid date format", expected="YYYY-MM-DD", actual=value)
        return False

    return True


def validate_datetime_field(
    result: ValidationResult,
    data: Dict[str, Any],
    field_name: str,
    entity_type: str,
    path_prefix: str = "",
    required: bool = False,
) -> bool:
    """Validate an ISO 8601 datetime field."""
    full_path = f"{path_prefix}.{field_name}" if path_prefix else field_name
    value = data.get(field_name)

    if value is None or value == "":
        if required:
            result.add_error(entity_type, full_path, "Required field is missing")
            return False
        return True

    if not isinstance(value, str):
        result.add_error(entity_type, full_path, "Expected a datetime string", actual=type(value).__name__)
        return False

    # Try multiple ISO 8601 formats
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ]

    for fmt in formats:
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue

    result.add_error(entity_type, full_path, "Invalid datetime format", expected="ISO 8601", actual=value)
    return False


def validate_email_field(
    result: ValidationResult,
    data: Dict[str, Any],
    field_name: str,
    entity_type: str,
    path_prefix: str = "",
    required: bool = False,
) -> bool:
    """Validate an email field."""
    full_path = f"{path_prefix}.{field_name}" if path_prefix else field_name
    value = data.get(field_name)

    if value is None or value == "":
        if required:
            result.add_error(entity_type, full_path, "Required field is missing")
            return False
        return True

    if not isinstance(value, str):
        result.add_error(entity_type, full_path, "Expected an email string", actual=type(value).__name__)
        return False

    # Simple email regex
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(email_pattern, value):
        result.add_error(entity_type, full_path, "Invalid email format", actual=value)
        return False

    return True


def validate_boolean_field(
    result: ValidationResult,
    data: Dict[str, Any],
    field_name: str,
    entity_type: str,
    path_prefix: str = "",
    required: bool = False,
) -> bool:
    """Validate a boolean field."""
    full_path = f"{path_prefix}.{field_name}" if path_prefix else field_name
    value = data.get(field_name)

    if value is None:
        if required:
            result.add_error(entity_type, full_path, "Required field is missing")
            return False
        return True

    if not isinstance(value, bool):
        result.add_error(entity_type, full_path, "Expected a boolean", actual=type(value).__name__)
        return False

    return True


def validate_integer_array(
    result: ValidationResult,
    data: Dict[str, Any],
    field_name: str,
    entity_type: str,
    path_prefix: str = "",
    required: bool = False,
) -> bool:
    """Validate that a field is an array of integers (no nulls)."""
    full_path = f"{path_prefix}.{field_name}" if path_prefix else field_name
    value = data.get(field_name)

    if value is None:
        if required:
            result.add_error(entity_type, full_path, "Required field is missing")
            return False
        return True

    if not isinstance(value, list):
        result.add_error(entity_type, full_path, "Expected an array", actual=type(value).__name__)
        return False

    valid = True
    for i, item in enumerate(value):
        if item is None:
            result.add_error(
                entity_type, f"{full_path}[{i}]",
                "Null values not allowed in integer array",
                severity=ErrorSeverity.ERROR
            )
            valid = False
        elif not isinstance(item, int) or isinstance(item, bool):
            result.add_error(
                entity_type, f"{full_path}[{i}]",
                "Expected an integer",
                actual=type(item).__name__
            )
            valid = False

    return valid


def validate_address(
    result: ValidationResult,
    address: Optional[Dict[str, Any]],
    entity_type: str,
    path_prefix: str,
    required: bool = False,
) -> bool:
    """Validate an address object."""
    if address is None:
        if required:
            result.add_error(entity_type, path_prefix, "Required address is missing")
            return False
        return True

    if not isinstance(address, dict):
        result.add_error(entity_type, path_prefix, "Address must be an object", actual=type(address).__name__)
        return False

    valid = True

    # Validate state code if present
    state = address.get("stateCode") or address.get("state")
    if state and state not in US_STATE_CODES:
        result.add_error(
            entity_type, f"{path_prefix}.state/stateCode",
            "Invalid US state code",
            expected=f"one of {US_STATE_CODES[:5]}...",
            actual=state,
        )
        valid = False

    # Validate postal code format (5 digits or 5+4 format)
    # Schema pattern: ^[0-9]{5}(-[0-9]{4})?$
    postal_code = address.get("postalCode") or address.get("postal_code") or address.get("zipCode")
    if postal_code and isinstance(postal_code, str) and postal_code.strip():
        postal_pattern = r'^[0-9]{5}(-[0-9]{4})?$'
        if not re.match(postal_pattern, postal_code):
            result.add_error(
                entity_type, f"{path_prefix}.postalCode",
                "Invalid postal code format",
                expected="5 digits (12345) or 5+4 format (12345-6789)",
                actual=postal_code,
            )
            valid = False

    return valid


# =============================================================================
# Entity Validators
# =============================================================================

def validate_project_info(result: ValidationResult, data: Dict[str, Any]):
    """Validate project-level fields."""
    entity_type = "Project"

    validate_string_field(result, data, "project_name", entity_type, required=True)
    validate_string_field(result, data, "project_description", entity_type, required=True)


def validate_loan_core(result: ValidationResult, data: Dict[str, Any]):
    """Validate LoanCore entity fields."""
    entity_type = "LoanCore"
    loan = data.get("loan", {})

    if not loan:
        result.add_error(entity_type, "loan", "Required 'loan' object is missing")
        return
    if not isinstance(loan, dict):
        result.add_error(entity_type, "loan", "Loan must be an object", actual=type(loan).__name__)
        return

    # Required fields
    validate_string_field(result, loan, "loanNumber", entity_type, path_prefix="loan", required=True)
    validate_enum_field(result, loan, "loanPurpose", LOAN_PURPOSES, entity_type, path_prefix="loan", required=True)
    validate_numeric_field(result, loan, "loanAmount", entity_type, path_prefix="loan", min_val=0, required=True)

    # Required for LoanDetails entity - principalAndInterestPayment
    validate_numeric_field(result, loan, "principalAndInterestPayment", entity_type, path_prefix="loan", min_val=0, required=True)
    pi_payment = loan.get("principalAndInterestPayment")
    if pi_payment is not None and pi_payment == 0:
        result.add_error(
            entity_type, "loan.principalAndInterestPayment",
            "principalAndInterestPayment cannot be 0 - must be a positive value",
            severity=ErrorSeverity.ERROR,
            actual=pi_payment,
        )

    # Optional but validated fields
    validate_enum_field(result, loan, "loanType", LOAN_TYPES, entity_type, path_prefix="loan")
    validate_enum_field(result, loan, "currentLoanStage", LOAN_STATUSES, entity_type, path_prefix="loan")
    validate_datetime_field(result, loan, "createdAt", entity_type, path_prefix="loan")

    # Required for LoanDetails entity - closingDate
    validate_date_field(result, loan, "closingDate", entity_type, path_prefix="loan", required=True)
    closing_date = loan.get("closingDate")
    if closing_date is None or closing_date == "":
        result.add_error(
            entity_type, "loan.closingDate",
            "closingDate is required for LoanDetails entity - must be a valid date in YYYY-MM-DD format",
            severity=ErrorSeverity.ERROR,
            actual=closing_date,
        )

    validate_string_field(result, loan, "loanOfficer", entity_type, path_prefix="loan")
    validate_boolean_field(result, loan, "borrowersWillPayTIFromEscrowIndicator", entity_type, path_prefix="loan")

    # LTV fields
    validate_numeric_field(result, loan, "ltvPercent", entity_type, path_prefix="loan", min_val=0, max_val=100)
    validate_numeric_field(result, loan, "cltvPercent", entity_type, path_prefix="loan", min_val=0, max_val=100)
    validate_numeric_field(result, loan, "hcltvPercent", entity_type, path_prefix="loan", min_val=0, max_val=100)

    # Loan Product validation
    loan_product = loan.get("loanProduct", {})
    if loan_product:
        # Handle case where loanProduct is a string instead of an object
        if isinstance(loan_product, str):
            result.add_error(
                entity_type, "loan.loanProduct",
                "loanProduct should be an object, not a string",
                severity=ErrorSeverity.WARNING,
                actual=loan_product,
            )
        elif isinstance(loan_product, dict):
            validate_enum_field(result, loan_product, "lienType", LIEN_TYPES, entity_type, path_prefix="loan.loanProduct")
            validate_enum_field(result, loan_product, "mortgageType", LOAN_TYPES, entity_type, path_prefix="loan.loanProduct")
            validate_enum_field(result, loan_product, "loanAmortizationType", AMORTIZATION_TYPES, entity_type, path_prefix="loan.loanProduct")
            validate_numeric_field(result, loan_product, "pricingEngineNoteRate", entity_type, path_prefix="loan.loanProduct", min_val=0, max_val=100)
            validate_numeric_field(result, loan_product, "apr", entity_type, path_prefix="loan.loanProduct", min_val=0, max_val=100)
            validate_integer_field(result, loan_product, "loanTermMonthsCount", entity_type, path_prefix="loan.loanProduct", min_val=1, max_val=480)

            # ARM fields
            validate_enum_field(result, loan_product, "armIndexType", ARM_INDEX_TYPES, entity_type, path_prefix="loan.loanProduct")
            validate_numeric_field(result, loan_product, "armMarginPercent", entity_type, path_prefix="loan.loanProduct", min_val=0, max_val=100)

    # Down Payment validation
    down_payment = loan.get("downPayment", {})
    if down_payment:
        if not isinstance(down_payment, dict):
            result.add_error(entity_type, "loan.downPayment", "downPayment must be an object")
        else:
            validate_numeric_field(result, down_payment, "amount", entity_type, path_prefix="loan.downPayment", min_val=0)
            validate_numeric_field(result, down_payment, "percentage", entity_type, path_prefix="loan.downPayment", min_val=0, max_val=100)

    # Closing Costs validation
    closing_costs = loan.get("closingCosts", [])
    if closing_costs:
        if not isinstance(closing_costs, list):
            result.add_error(entity_type, "loan.closingCosts", "closingCosts must be an array")
            return
        for i, cost in enumerate(closing_costs):
            path = f"loan.closingCosts[{i}]"
            if not isinstance(cost, dict):
                result.add_error(entity_type, path, "Closing cost must be an object")
                continue
            validate_numeric_field(result, cost, "amount", entity_type, path_prefix=path, min_val=0)
            validate_boolean_field(result, cost, "isAPRFeeIndicator", entity_type, path_prefix=path)


def validate_borrowers(result: ValidationResult, data: Dict[str, Any]):
    """Validate Borrower entity fields."""
    entity_type = "Borrower"
    borrowers = data.get("borrowers", [])

    if not borrowers:
        result.add_error(entity_type, "borrowers", "Required 'borrowers' array is missing or empty")
        return

    if not isinstance(borrowers, list):
        result.add_error(entity_type, "borrowers", "Borrowers must be an array", actual=type(borrowers).__name__)
        return

    for idx, borrower in enumerate(borrowers):
        path_prefix = f"borrowers[{idx}]"

        if not isinstance(borrower, dict):
            result.add_error(entity_type, path_prefix, "Borrower must be an object", actual=type(borrower).__name__)
            continue

        # Required fields
        validate_string_field(result, borrower, "firstName", entity_type, path_prefix=path_prefix, required=True, max_length=100)
        validate_string_field(result, borrower, "lastName", entity_type, path_prefix=path_prefix, required=True, max_length=100)

        # Optional identity fields
        validate_string_field(result, borrower, "middleName", entity_type, path_prefix=path_prefix, max_length=100)
        validate_enum_field(result, borrower, "suffix", NAME_SUFFIXES, entity_type, path_prefix=path_prefix)
        validate_email_field(result, borrower, "emailAddress", entity_type, path_prefix=path_prefix)
        validate_date_field(result, borrower, "dateOfBirth", entity_type, path_prefix=path_prefix)

        # Citizenship and Status
        validate_enum_field(result, borrower, "citizenshipType", CITIZENSHIP_STATUSES, entity_type, path_prefix=path_prefix)
        validate_enum_field(result, borrower, "maritalStatus", MARITAL_STATUSES, entity_type, path_prefix=path_prefix)

        # Dependents
        validate_integer_field(result, borrower, "dependentCount", entity_type, path_prefix=path_prefix, min_val=0)
        validate_integer_array(result, borrower, "dependentAges", entity_type, path_prefix=path_prefix)

        # HMDA fields
        validate_enum_field(result, borrower, "hmdaEthnicity", HMDA_ETHNICITIES, entity_type, path_prefix=path_prefix)
        validate_enum_array(result, borrower, "hmdaRace", HMDA_RACES, entity_type, path_prefix=path_prefix)
        validate_enum_field(result, borrower, "hmdaSex", HMDA_SEXES, entity_type, path_prefix=path_prefix)

        # Boolean fields
        validate_boolean_field(result, borrower, "intentToOccupy", entity_type, path_prefix=path_prefix)
        validate_boolean_field(result, borrower, "firstTimeHomebuyerIndicator", entity_type, path_prefix=path_prefix)
        validate_boolean_field(result, borrower, "mailingAddressSameAsCurrent", entity_type, path_prefix=path_prefix)

        # Addresses
        validate_address(result, borrower.get("currentAddress"), entity_type, f"{path_prefix}.currentAddress")
        validate_address(result, borrower.get("mailingAddress"), entity_type, f"{path_prefix}.mailingAddress")

        # Declarations
        declarations = borrower.get("declarations", {})
        if declarations and isinstance(declarations, dict):
            decl_path = f"{path_prefix}.declarations"
            for field in ["ownershipInterestInPropertyLast3Years", "outstandingJudgments", "declaredBankruptcy",
                         "propertyForeclosed", "partyToLawsuit", "conveyedTitleInLieu", "preforeclosureShortSale",
                         "delinquentOnFederalDebt", "obligatedOnOtherLoan", "alimonyChildSupport",
                         "coSignerOnOtherDebt", "borrowedDownPayment"]:
                validate_boolean_field(result, declarations, field, entity_type, path_prefix=decl_path)

        # Income totals
        validate_numeric_field(result, borrower, "totalNetIncome", entity_type, path_prefix=path_prefix, min_val=0)
        validate_numeric_field(result, borrower, "totalTaxableIncome", entity_type, path_prefix=path_prefix, min_val=0)
        validate_numeric_field(result, borrower, "totalNonTaxableIncome", entity_type, path_prefix=path_prefix, min_val=0)

        # Validate nested arrays
        validate_assets(result, borrower.get("assets", []), idx)
        validate_incomes(result, borrower.get("incomes", []), idx)
        validate_liabilities(result, borrower.get("liabilities", []), idx)
        validate_credit_info(result, borrower, idx)


def validate_assets(result: ValidationResult, assets: List[Dict[str, Any]], borrower_idx: int):
    """Validate Asset entities for a borrower."""
    entity_type = "Asset"

    if not isinstance(assets, list):
        result.add_error(entity_type, f"borrowers[{borrower_idx}].assets", "Assets must be an array")
        return

    for idx, asset in enumerate(assets):
        path_prefix = f"borrowers[{borrower_idx}].assets[{idx}]"

        if not isinstance(asset, dict):
            result.add_error(entity_type, path_prefix, "Asset must be an object")
            continue

        # Required field
        validate_enum_field(result, asset, "assetType", ASSET_TYPES, entity_type, path_prefix=path_prefix, required=True)

        # Optional fields
        validate_string_field(result, asset, "institutionName", entity_type, path_prefix=path_prefix, max_length=255)
        validate_string_field(result, asset, "accountNumber", entity_type, path_prefix=path_prefix, max_length=20)
        validate_numeric_field(result, asset, "currentBalance", entity_type, path_prefix=path_prefix, min_val=0)
        validate_boolean_field(result, asset, "isLiquid", entity_type, path_prefix=path_prefix)
        validate_boolean_field(result, asset, "isGift", entity_type, path_prefix=path_prefix)
        validate_boolean_field(result, asset, "willBeUsedForClosing", entity_type, path_prefix=path_prefix)

        # Institution address
        validate_address(result, asset.get("institutionAddress"), entity_type, f"{path_prefix}.institutionAddress")


def validate_incomes(result: ValidationResult, incomes: List[Dict[str, Any]], borrower_idx: int):
    """Validate Income entities for a borrower."""
    entity_type = "Income"

    if not isinstance(incomes, list):
        result.add_error(entity_type, f"borrowers[{borrower_idx}].incomes", "Incomes must be an array")
        return

    for idx, income in enumerate(incomes):
        path_prefix = f"borrowers[{borrower_idx}].incomes[{idx}]"

        if not isinstance(income, dict):
            result.add_error(entity_type, path_prefix, "Income must be an object")
            continue

        # Required field
        validate_enum_field(result, income, "incomeType", INCOME_TYPES, entity_type, path_prefix=path_prefix, required=True)

        # Optional fields
        validate_enum_field(result, income, "frequency", INCOME_FREQUENCIES, entity_type, path_prefix=path_prefix)
        validate_enum_field(result, income, "documentationLevel", DOCUMENTATION_LEVELS, entity_type, path_prefix=path_prefix)
        validate_enum_field(result, income, "verificationStatus", VERIFICATION_STATUSES, entity_type, path_prefix=path_prefix)
        validate_numeric_field(result, income, "totalCalculatedQualifiedMonthlyIncome", entity_type, path_prefix=path_prefix, min_val=0)
        validate_numeric_field(result, income, "totalCalculatedStatedMonthlyIncome", entity_type, path_prefix=path_prefix, min_val=0)
        validate_boolean_field(result, income, "foreignIncomeIndicator", entity_type, path_prefix=path_prefix)
        validate_boolean_field(result, income, "seasonalIncomeIndicator", entity_type, path_prefix=path_prefix)
        validate_boolean_field(result, income, "taxableIndicator", entity_type, path_prefix=path_prefix)
        validate_boolean_field(result, income, "isPrimary", entity_type, path_prefix=path_prefix)

        # Stated/Qualified income
        for income_field in ["statedEmploymentBaseIncome", "qualifiedEmploymentBaseIncome"]:
            income_obj = income.get(income_field, {})
            if income_obj:
                if not isinstance(income_obj, dict):
                    result.add_error(entity_type, f"{path_prefix}.{income_field}", "Income value must be an object")
                else:
                    validate_numeric_field(result, income_obj, "amount", entity_type, path_prefix=f"{path_prefix}.{income_field}", min_val=0)

        # Employer (for Employment entity)
        employer = income.get("employer", {})
        if employer:
            validate_employment(result, employer, borrower_idx, idx)


def validate_employment(result: ValidationResult, employer: Dict[str, Any], borrower_idx: int, income_idx: int):
    """Validate Employment entity fields from income.employer."""
    entity_type = "Employment"
    path_prefix = f"borrowers[{borrower_idx}].incomes[{income_idx}].employer"

    if not isinstance(employer, dict):
        result.add_error(entity_type, path_prefix, "Employer must be an object")
        return

    # Optional fields
    validate_string_field(result, employer, "name", entity_type, path_prefix=path_prefix, max_length=255)
    validate_string_field(result, employer, "jobTitle", entity_type, path_prefix=path_prefix, max_length=100)
    validate_date_field(result, employer, "startDate", entity_type, path_prefix=path_prefix)
    validate_date_field(result, employer, "endDate", entity_type, path_prefix=path_prefix)
    validate_enum_field(result, employer, "employmentClassificationType", EMPLOYMENT_CLASSIFICATION_TYPES, entity_type, path_prefix=path_prefix)
    validate_enum_field(result, employer, "employmentStatus", EMPLOYMENT_STATUSES, entity_type, path_prefix=path_prefix)
    validate_boolean_field(result, employer, "isSelfEmployed", entity_type, path_prefix=path_prefix)
    validate_boolean_field(result, employer, "isEmployedByFamilyMember", entity_type, path_prefix=path_prefix)
    validate_boolean_field(result, employer, "specialBorrowerEmployerRelationshipIndicator", entity_type, path_prefix=path_prefix)
    validate_integer_field(result, employer, "numberOfMonthsInThisLineOfWork", entity_type, path_prefix=path_prefix, min_val=0)
    validate_numeric_field(result, employer, "yearsInProfession", entity_type, path_prefix=path_prefix, min_val=0)

    # Employer address
    validate_address(result, employer.get("address"), entity_type, f"{path_prefix}.address")


def validate_liabilities(result: ValidationResult, liabilities: List[Dict[str, Any]], borrower_idx: int):
    """Validate Liability entities for a borrower."""
    entity_type = "Liability"

    if not isinstance(liabilities, list):
        result.add_error(entity_type, f"borrowers[{borrower_idx}].liabilities", "Liabilities must be an array")
        return

    for idx, liability in enumerate(liabilities):
        path_prefix = f"borrowers[{borrower_idx}].liabilities[{idx}]"

        if not isinstance(liability, dict):
            result.add_error(entity_type, path_prefix, "Liability must be an object")
            continue

        # Required field
        validate_enum_field(result, liability, "liabilityType", LIABILITY_TYPES, entity_type, path_prefix=path_prefix, required=True)

        # Optional fields
        validate_string_field(result, liability, "creditorName", entity_type, path_prefix=path_prefix, max_length=255)
        validate_string_field(result, liability, "accountNumber", entity_type, path_prefix=path_prefix, max_length=20)
        validate_numeric_field(result, liability, "unpaidBalance", entity_type, path_prefix=path_prefix, min_val=0)
        validate_numeric_field(result, liability, "monthlyPaymentAmount", entity_type, path_prefix=path_prefix, min_val=0)
        validate_numeric_field(result, liability, "monthlyPayment", entity_type, path_prefix=path_prefix, min_val=0)
        validate_numeric_field(result, liability, "highCreditLimit", entity_type, path_prefix=path_prefix, min_val=0)
        validate_integer_field(result, liability, "monthsRemaining", entity_type, path_prefix=path_prefix, min_val=0)
        validate_integer_field(result, liability, "lienPosition", entity_type, path_prefix=path_prefix, min_val=1, max_val=4)
        validate_boolean_field(result, liability, "willBePaidOffAtClosing", entity_type, path_prefix=path_prefix)
        validate_boolean_field(result, liability, "isExcludedFromDti", entity_type, path_prefix=path_prefix)
        validate_boolean_field(result, liability, "isSubjectPropertyLien", entity_type, path_prefix=path_prefix)


def validate_credit_info(result: ValidationResult, borrower: Dict[str, Any], borrower_idx: int):
    """Validate CreditReport entity fields from borrower."""
    entity_type = "CreditReport"
    path_prefix = f"borrowers[{borrower_idx}]"

    # Credit scores array
    credit_scores = borrower.get("creditScores", [])
    has_valid_credit_score = False

    if not credit_scores:
        result.add_error(
            entity_type, f"{path_prefix}.creditScores",
            "creditScores array is missing or empty - at least one credit score is required for LoanDetails entity",
            severity=ErrorSeverity.ERROR
        )
    elif not isinstance(credit_scores, list):
        result.add_error(entity_type, f"{path_prefix}.creditScores", "creditScores must be an array")
    else:
        for idx, score in enumerate(credit_scores):
            score_path = f"{path_prefix}.creditScores[{idx}]"
            if not isinstance(score, dict):
                result.add_error(entity_type, score_path, "Credit score must be an object")
                continue
            validate_enum_field(result, score, "bureau", CREDIT_BUREAUS, entity_type, path_prefix=score_path, required=True)
            validate_integer_field(result, score, "score", entity_type, path_prefix=score_path, min_val=300, max_val=850)
            # creditModelType does not accept empty strings - must be a valid value or null/missing
            validate_enum_field(result, score, "creditModelType", SCORE_MODELS, entity_type, path_prefix=score_path, allow_empty_string=False)

            # Track if we have at least one valid score
            if score.get("score") is not None:
                has_valid_credit_score = True

        # Check if at least one credit score has a valid score value
        if not has_valid_credit_score:
            result.add_error(
                entity_type, f"{path_prefix}.creditScores",
                "All credit scores have null values - at least one credit score with a valid score is required for LoanDetails entity",
                severity=ErrorSeverity.ERROR
            )

    # Median credit score - required for LoanDetails entity
    median_score = borrower.get("medianCreditScore", {})
    if not median_score:
        result.add_error(
            entity_type, f"{path_prefix}.medianCreditScore",
            "medianCreditScore is missing - required for LoanDetails entity",
            severity=ErrorSeverity.ERROR
        )
    else:
        median_path = f"{path_prefix}.medianCreditScore"
        if not isinstance(median_score, dict):
            result.add_error(entity_type, median_path, "medianCreditScore must be an object")
            return
        validate_enum_field(result, median_score, "bureau", CREDIT_BUREAUS, entity_type, path_prefix=median_path)
        validate_integer_field(result, median_score, "score", entity_type, path_prefix=median_path, min_val=300, max_val=850)
        # creditModelType does not accept empty strings - must be a valid value or null/missing
        validate_enum_field(result, median_score, "creditModelType", SCORE_MODELS, entity_type, path_prefix=median_path, allow_empty_string=False)

        # Ensure medianCreditScore.score is not null
        if median_score.get("score") is None:
            result.add_error(
                entity_type, f"{median_path}.score",
                "medianCreditScore.score is null - a valid representative credit score is required for LoanDetails entity",
                severity=ErrorSeverity.ERROR
            )

    # Other credit fields
    validate_enum_field(result, borrower, "creditReportType", REPORT_TYPES, entity_type, path_prefix=path_prefix)
    validate_date_field(result, borrower, "creditReportIssuedDate", entity_type, path_prefix=path_prefix)
    validate_integer_field(result, borrower, "openTradelines", entity_type, path_prefix=path_prefix, min_val=0)
    validate_integer_field(result, borrower, "totalTradelines", entity_type, path_prefix=path_prefix, min_val=0)


def validate_subject_property(result: ValidationResult, data: Dict[str, Any]):
    """Validate SubjectProperty entity fields."""
    entity_type = "SubjectProperty"
    subject_property = data.get("subject_property", {})

    if not subject_property:
        result.add_error(
            entity_type, "subject_property",
            "Required 'subject_property' object is missing",
            severity=ErrorSeverity.WARNING
        )
        return
    if not isinstance(subject_property, dict):
        result.add_error(entity_type, "subject_property", "Subject property must be an object")
        return

    path_prefix = "subject_property"

    # Required field - either intendedUsageType or occupancyType
    intended_usage = subject_property.get("intendedUsageType")
    occupancy = subject_property.get("occupancyType")

    if not intended_usage and not occupancy:
        result.add_error(
            entity_type, f"{path_prefix}.intendedUsageType/occupancyType",
            "Either 'intendedUsageType' or 'occupancyType' is required"
        )
    else:
        validate_enum_field(result, subject_property, "intendedUsageType", OCCUPANCY_TYPES, entity_type, path_prefix=path_prefix)
        validate_enum_field(result, subject_property, "occupancyType", OCCUPANCY_TYPES, entity_type, path_prefix=path_prefix)

    # Property characteristics
    validate_enum_field(result, subject_property, "propertyType", PROPERTY_TYPES, entity_type, path_prefix=path_prefix)
    validate_enum_field(result, subject_property, "constructionType", CONSTRUCTION_TYPES, entity_type, path_prefix=path_prefix)
    validate_enum_field(result, subject_property, "manufacturedWidthType", MANUFACTURED_WIDTH_TYPES, entity_type, path_prefix=path_prefix)
    validate_enum_field(result, subject_property, "condoProjectClassification", CONDO_PROJECT_CLASSIFICATIONS, entity_type, path_prefix=path_prefix)
    # titleMannerHeld does not accept empty strings - must be a valid value or null/missing
    validate_enum_field(result, subject_property, "titleMannerHeld", TITLE_MANNER_HELD, entity_type, path_prefix=path_prefix, allow_empty_string=False)

    # Numeric fields
    validate_integer_field(result, subject_property, "numberOfUnits", entity_type, path_prefix=path_prefix, min_val=1, max_val=4)
    validate_integer_field(result, subject_property, "yearBuilt", entity_type, path_prefix=path_prefix, min_val=1600, max_val=2100)
    validate_numeric_field(result, subject_property, "lotSizeAcres", entity_type, path_prefix=path_prefix, min_val=0)
    validate_numeric_field(result, subject_property, "lotSizeSqft", entity_type, path_prefix=path_prefix, min_val=0)
    validate_numeric_field(result, subject_property, "livingAreaSqft", entity_type, path_prefix=path_prefix, min_val=0)
    validate_integer_field(result, subject_property, "bedrooms", entity_type, path_prefix=path_prefix, min_val=0)
    validate_numeric_field(result, subject_property, "bathrooms", entity_type, path_prefix=path_prefix, min_val=0)

    # Value fields
    # purchasePrice is required for LoanDetails entity
    validate_numeric_field(result, subject_property, "purchasePrice", entity_type, path_prefix=path_prefix, min_val=0, required=True)
    purchase_price = subject_property.get("purchasePrice")
    if purchase_price is None:
        result.add_error(
            entity_type, f"{path_prefix}.purchasePrice",
            "Required field 'purchasePrice' is missing - needed for LoanDetails entity",
            severity=ErrorSeverity.ERROR
        )
    elif purchase_price == 0:
        result.add_error(
            entity_type, f"{path_prefix}.purchasePrice",
            "purchasePrice cannot be 0 - must be a positive value",
            severity=ErrorSeverity.ERROR,
            actual=purchase_price,
        )

    validate_numeric_field(result, subject_property, "estimatedValueAmount", entity_type, path_prefix=path_prefix, min_val=0)
    validate_numeric_field(result, subject_property, "appraisedValue", entity_type, path_prefix=path_prefix, min_val=0)
    validate_numeric_field(result, subject_property, "improvementsCost", entity_type, path_prefix=path_prefix, min_val=0)
    validate_numeric_field(result, subject_property, "landValue", entity_type, path_prefix=path_prefix, min_val=0)
    validate_numeric_field(result, subject_property, "hoaMonthlyDues", entity_type, path_prefix=path_prefix, min_val=0)
    validate_numeric_field(result, subject_property, "mixedUseCommercialPercent", entity_type, path_prefix=path_prefix, min_val=0, max_val=100)

    # Required for LoanDetails entity: appraisalValue must be present
    # LoanDetails entity requires propertyInformation.appraisalValue
    # The entity creator looks for appraisedValue or appraisalValue
    appraised_value = subject_property.get("appraisedValue") or subject_property.get("appraisalValue")
    if appraised_value is None:
        result.add_error(
            entity_type, f"{path_prefix}.appraisedValue",
            "Required field 'appraisedValue' (or 'appraisalValue') is missing - needed for LoanDetails entity",
            severity=ErrorSeverity.ERROR
        )

    # Date fields
    validate_date_field(result, subject_property, "appraisalDate", entity_type, path_prefix=path_prefix)

    # Boolean fields
    validate_boolean_field(result, subject_property, "mixedUsageIndicator", entity_type, path_prefix=path_prefix)
    validate_boolean_field(result, subject_property, "isPUD", entity_type, path_prefix=path_prefix)
    validate_boolean_field(result, subject_property, "isCondo", entity_type, path_prefix=path_prefix)
    validate_boolean_field(result, subject_property, "isMixedUse", entity_type, path_prefix=path_prefix)
    validate_boolean_field(result, subject_property, "isManufactured", entity_type, path_prefix=path_prefix)
    validate_boolean_field(result, subject_property, "isInSpecialFloodHazardArea", entity_type, path_prefix=path_prefix)
    validate_boolean_field(result, subject_property, "energyEfficientIndicator", entity_type, path_prefix=path_prefix)
    validate_boolean_field(result, subject_property, "ruralAreaIndicator", entity_type, path_prefix=path_prefix)

    # Address
    validate_address(result, subject_property.get("address"), entity_type, f"{path_prefix}.address")


def validate_properties(result: ValidationResult, data: Dict[str, Any]):
    """Validate Real Estate Owned (REO) properties."""
    entity_type = "RealEstateOwned"
    properties = data.get("properties", [])

    if not properties:
        return  # Properties are optional

    if not isinstance(properties, list):
        result.add_error(entity_type, "properties", "Properties must be an array")
        return

    for idx, prop in enumerate(properties):
        path_prefix = f"properties[{idx}]"

        if not isinstance(prop, dict):
            result.add_error(entity_type, path_prefix, "Property must be an object")
            continue

        # Validate property type
        validate_enum_field(result, prop, "propertyType", PROPERTY_TYPES, entity_type, path_prefix=path_prefix)

        # Other fields
        validate_enum_field(result, prop, "occupancyType", OCCUPANCY_TYPES, entity_type, path_prefix=path_prefix)
        validate_numeric_field(result, prop, "marketValue", entity_type, path_prefix=path_prefix, min_val=0)
        validate_numeric_field(result, prop, "presentValue", entity_type, path_prefix=path_prefix, min_val=0)
        validate_numeric_field(result, prop, "purchasePrice", entity_type, path_prefix=path_prefix, min_val=0)
        validate_boolean_field(result, prop, "isSubjectProperty", entity_type, path_prefix=path_prefix)

        # Address
        validate_address(result, prop.get("address"), entity_type, f"{path_prefix}.address")

        # Mortgages
        mortgages = prop.get("mortgages", [])
        if mortgages and not isinstance(mortgages, list):
            result.add_error(entity_type, f"{path_prefix}.mortgages", "Mortgages must be an array")
        elif mortgages:
            for midx, mortgage in enumerate(mortgages):
                mort_path = f"{path_prefix}.mortgages[{midx}]"
                if not isinstance(mortgage, dict):
                    result.add_error(entity_type, mort_path, "Mortgage must be an object")
                    continue
                validate_numeric_field(result, mortgage, "unpaidBalance", entity_type, path_prefix=mort_path, min_val=0)
                validate_numeric_field(result, mortgage, "monthlyPayment", entity_type, path_prefix=mort_path, min_val=0)
                validate_integer_field(result, mortgage, "lienPosition", entity_type, path_prefix=mort_path, min_val=1)


# =============================================================================
# Main Validation Function
# =============================================================================

def validate_input(data: Dict[str, Any]) -> ValidationResult:
    """Run all validations on input data."""
    result = ValidationResult()
    if not isinstance(data, dict):
        result.add_error("Input", "$", "JSON root must be an object", actual=type(data).__name__)
        return result

    # Project info
    validate_project_info(result, data)

    # LoanCore entity
    validate_loan_core(result, data)

    # Borrower entities (includes Assets, Incomes, Liabilities, Employment, CreditReport)
    validate_borrowers(result, data)

    # SubjectProperty entity
    validate_subject_property(result, data)

    # Real Estate Owned properties
    validate_properties(result, data)

    return result


def print_results(result: ValidationResult, verbose: bool = False):
    """Print validation results in a formatted way."""
    print("\n" + "=" * 80)
    print("INPUT VALIDATION RESULTS")
    print("=" * 80)

    if not result.errors:
        print("\n✅ All validations passed! No errors found.\n")
        return

    # Group errors by entity type
    errors_by_entity: Dict[str, List[ValidationError]] = {}
    for error in result.errors:
        if error.entity_type not in errors_by_entity:
            errors_by_entity[error.entity_type] = []
        errors_by_entity[error.entity_type].append(error)

    # Summary
    print(f"\n📊 Summary:")
    print(f"   Errors:   {result.error_count}")
    print(f"   Warnings: {result.warning_count}")
    print(f"   Info:     {result.info_count}")
    print(f"   Total:    {len(result.errors)}")

    # Print by entity type
    for entity_type in sorted(errors_by_entity.keys()):
        errors = errors_by_entity[entity_type]
        error_count = sum(1 for e in errors if e.severity == ErrorSeverity.ERROR)
        warning_count = sum(1 for e in errors if e.severity == ErrorSeverity.WARNING)

        print(f"\n{'─' * 80}")
        print(f"📋 {entity_type} ({error_count} errors, {warning_count} warnings)")
        print(f"{'─' * 80}")

        for error in errors:
            severity_icon = "❌" if error.severity == ErrorSeverity.ERROR else "⚠️" if error.severity == ErrorSeverity.WARNING else "ℹ️"
            print(f"\n  {severity_icon} {error.field_path}")
            print(f"     └─ {error.message}")
            if error.expected:
                print(f"        Expected: {error.expected}")
            if error.actual is not None:
                actual_str = str(error.actual)
                if len(actual_str) > 60:
                    actual_str = actual_str[:60] + "..."
                print(f"        Got: {actual_str}")

    print("\n" + "=" * 80)

    if result.has_errors:
        print("❌ Validation FAILED - Fix the errors above before running add_loan_mock_data.py")
    else:
        print("⚠️  Validation passed with warnings - You may proceed but review the warnings")

    print("=" * 80 + "\n")


# =============================================================================
# Main Function
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Validate input.json against entity creator requirements",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python validate_input.py --input input.json
  python validate_input.py --input input.json --verbose
  python validate_input.py --input sample_loan_input.json

This script validates:
  - Project info (project_name, project_description)
  - LoanCore entity fields (loan object)
  - Borrower entity fields (borrowers array)
  - Asset entity fields (borrowers[].assets)
  - Income entity fields (borrowers[].incomes)
  - Liability entity fields (borrowers[].liabilities)
  - Employment entity fields (borrowers[].incomes[].employer)
  - CreditReport entity fields (borrowers[] credit fields)
  - SubjectProperty entity fields (subject_property object)
  - RealEstateOwned properties (properties array)
        """,
    )

    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to the input JSON file to validate",
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show verbose output including info-level messages",
    )

    parser.add_argument(
        "--json-output",
        action="store_true",
        help="Output results as JSON instead of formatted text",
    )

    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the JSON validation report",
    )

    args = parser.parse_args()

    # Validate input file exists
    if not args.input.is_file():
        print(f"❌ Input file not found: {args.input}")
        return 1

    # Load and parse JSON
    try:
        with open(args.input, "r", encoding="utf-8") as f:
            input_data = json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        print(f"❌ Could not read valid JSON from input file: {e}")
        return 1
    if not isinstance(input_data, dict):
        print("❌ Input JSON root must be an object")
        return 1

    # Run validation
    result = validate_input(input_data)

    # Build output data structure
    output = {
        "input_file": str(args.input),
        "valid": not result.has_errors,
        "error_count": result.error_count,
        "warning_count": result.warning_count,
        "info_count": result.info_count,
        "errors": [
            {
                "entity_type": e.entity_type,
                "field_path": e.field_path,
                "message": e.message,
                "severity": e.severity.value,
                "expected": e.expected,
                "actual": str(e.actual) if e.actual is not None else None,
            }
            for e in result.errors
        ],
    }

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
            f.write("\n")
        print(f"\n📄 Validation results saved to: {args.output}")

    # Output results
    if args.json_output:
        print(json.dumps(output, indent=2))
    else:
        print_results(result, verbose=args.verbose)

    # Exit with appropriate code
    return 1 if result.has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
