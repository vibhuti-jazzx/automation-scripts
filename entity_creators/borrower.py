"""
Borrower entity payload builder.
"""

import logging
from datetime import date
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, EmailStr, Field, ValidationError

from .base import (
    BORROWER_ONTOLOGY_ID,
    build_provenance,
    generate_stable_uuid,
    get_current_timestamp,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Nested Models
# ============================================================================


class ResidenceAddress(BaseModel):
    """Residence address schema."""

    street: Optional[str] = None
    street_line_1: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = Field(default="US")
    address_type: Optional[str] = None


class Address(BaseModel):
    """Generic address schema."""

    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None


# ============================================================================
# Borrower Type Literals
# ============================================================================

BorrowerType = Literal["Primary", "CoBorrower", "NonBorrowingSpouse"]

CitizenshipStatus = Literal["USCitizen", "PermanentResidentAlien", "NonPermanentResidentAlien"]

MaritalStatus = Literal["Married", "Separated", "Unmarried"]

NameSuffix = Literal["Jr", "Sr", "II", "III", "IV"]

BankruptcyChapter = Literal["Chapter7", "Chapter11", "Chapter12", "Chapter13"]

OwnershipPropertyType = Literal[
    "PrimaryResidence", "SecondHome", "Investment", "FHA_SecondaryResidence"
]

HMDAEthnicity = Literal[
    "HispanicOrLatino",
    "NotHispanicOrLatino",
    "InformationNotProvided",
    "NotApplicable",
]

HMDARace = Literal[
    "AmericanIndianOrAlaskaNative",
    "Asian",
    "BlackOrAfricanAmerican",
    "NativeHawaiianOrOtherPacificIslander",
    "White",
    "InformationNotProvided",
    "NotApplicable",
]

HMDASex = Literal["Female", "Male", "InformationNotProvided", "NotApplicable"]


# ============================================================================
# JazzBorrower Model - MISMO Borrower entity ontology schema
# ============================================================================


class JazzBorrower(BaseModel):
    """
    Jazz Borrower entity with validation.

    This uses Pydantic's validation and transformation capabilities
    to validate borrower data before sending to the API.
    Follows the MISMO Borrower entity ontology schema.
    """

    # Required fields
    id: str
    borrower_type: BorrowerType
    borrower_sequence: int = Field(..., ge=1)

    # Basic Identity
    first_name: Optional[str] = Field(None, max_length=100)
    middle_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    suffix: Optional[NameSuffix] = None
    ssn_masked: Optional[str] = None
    date_of_birth: Optional[date] = None
    age_at_application: Optional[int] = Field(None, ge=18)

    # Citizenship and Marital Status
    citizenship_status: Optional[CitizenshipStatus] = None
    marital_status: Optional[MaritalStatus] = None
    dependent_count: Optional[int] = Field(None, ge=0)
    dependent_ages: List[int] = Field(default_factory=list)

    # Contact
    email: Optional[EmailStr] = None
    phone_home: Optional[str] = None
    phone_cell: Optional[str] = None
    phone_work: Optional[str] = None

    # Addresses
    current_address: Optional[ResidenceAddress] = None
    former_addresses: List[ResidenceAddress] = Field(default_factory=list)
    mailing_address: Optional[Address] = None
    mailing_address_same_as_current: Optional[bool] = None

    # Declarations
    is_first_time_homebuyer: Optional[bool] = None
    will_occupy_as_primary_residence: Optional[bool] = None
    ownership_interest_in_property_last_3_years: Optional[bool] = None
    ownership_interest_property_type: Optional[OwnershipPropertyType] = None

    # Legal/Financial Declarations
    has_outstanding_judgments: Optional[bool] = None
    has_declared_bankruptcy: Optional[bool] = None
    bankruptcy_chapter: Optional[BankruptcyChapter] = None
    bankruptcy_discharge_date: Optional[date] = None
    has_property_foreclosed: Optional[bool] = None
    foreclosure_date: Optional[date] = None
    is_party_to_lawsuit: Optional[bool] = None
    has_conveyed_title_in_lieu: Optional[bool] = None
    has_preforeclosure_short_sale: Optional[bool] = None
    is_delinquent_on_federal_debt: Optional[bool] = None
    is_obligated_on_other_loan: Optional[bool] = None
    has_alimony_child_support: Optional[bool] = None
    alimony_child_support_monthly_usd: Optional[float] = Field(None, ge=0)
    is_co_signer_on_other_debt: Optional[bool] = None
    has_borrowed_down_payment: Optional[bool] = None

    # Additional Citizenship Fields
    is_us_citizen: Optional[bool] = None
    is_permanent_resident_alien: Optional[bool] = None
    citizenship_country: Optional[str] = Field(None, description="Country of citizenship")

    # HMDA Fields
    hmda_ethnicity: Optional[List[HMDAEthnicity]] = None
    hmda_race: List[HMDARace] = Field(default_factory=list)
    hmda_sex: Optional[HMDASex] = None

    # Related IDs
    employment_ids: List[str] = Field(default_factory=list)
    income_ids: List[str] = Field(default_factory=list)
    asset_ids: List[str] = Field(default_factory=list)
    liability_ids: List[str] = Field(default_factory=list)
    reo_ids: List[str] = Field(default_factory=list)
    credit_report_id: Optional[str] = None

    # Provenance (optional, typically added by system)
    provenance: Optional[dict] = None


class BorrowerPayloadBuilder:
    """Builds payload for Borrower entities."""

    @staticmethod
    def build_all(
        collection_id: str,
        input_data: Dict[str, Any],
        description: str = "Borrower entity",
    ) -> List[Tuple[Dict[str, Any], str]]:
        """Build payloads for all borrowers in the input data.

        Args:
            collection_id: The collection ID from project creation.
            input_data: The full input JSON data.
            description: Description for the entities.

        Returns:
            A list of tuples (payload, external_id) for each borrower.
        """
        borrowers = input_data.get("borrowers", [])
        results = []
        for idx, borrower in enumerate(borrowers):
            try:
                payload, external_id = BorrowerPayloadBuilder.build_single(
                    collection_id=collection_id,
                    borrower=borrower,
                    borrower_index=idx,
                    description=description,
                )
                results.append((payload, external_id))
            except ValidationError as ve:
                logger.error(
                    f"Borrower validation failed for index {idx}: {ve.errors()}"
                )
                raise
        return results

    @staticmethod
    def build_single(
        collection_id: str,
        borrower: Dict[str, Any],
        borrower_index: int,
        description: str = "Borrower entity",
    ) -> Tuple[Dict[str, Any], str]:
        """Build the payload for a single Borrower entity.

        Args:
            collection_id: The collection ID from project creation.
            borrower: The borrower data from input.
            borrower_index: The index of the borrower (0 = primary).
            description: Description for the entity.

        Returns:
            A tuple of (payload dict, external_borrower_id).

        Raises:
            ValidationError: If the borrower data fails Pydantic validation.
        """
        now = get_current_timestamp()

        first_name = borrower.get("firstName", "")
        middle_name = borrower.get("middleName", "")
        last_name = borrower.get("lastName", "")
        entity_name = f"Borrower-{first_name}-{last_name}"

        # --- Current address mapping ---
        input_addr = borrower.get("currentAddress") or {}
        current_address = None
        if input_addr:
            current_address = ResidenceAddress(
                city=input_addr.get("city"),
                state=input_addr.get("stateCode", input_addr.get("state")),
                country="US",
                postal_code=input_addr.get("zipCode", input_addr.get("zip")),
                address_type="Current",
                street_line_1=input_addr.get("line", input_addr.get("street", input_addr.get("fullStreetAddress"))),
            )

        # --- Declarations mapping (camelCase → snake_case) ---
        input_decl = borrower.get("declarations") or {}
        has_outstanding_judgments = input_decl.get("outstandingJudgments", borrower.get("outstandingJudgmentsIndicator", False))
        has_declared_bankruptcy = input_decl.get("declaredBankruptcy", borrower.get("bankruptcyIndicator", False))
        has_property_foreclosed = input_decl.get("propertyForeclosed", borrower.get("priorPropertyForeclosureCompletedIndicator", False))
        is_party_to_lawsuit = input_decl.get("partyToLawsuit", borrower.get("partyToLawsuitIndicator", False))
        has_conveyed_title_in_lieu = input_decl.get("conveyedTitleInLieu", borrower.get("priorPropertyDeedInLieuConveyedIndicator", False))
        has_preforeclosure_short_sale = input_decl.get("preforeclosureShortSale", borrower.get("priorPropertyShortSaleCompletedIndicator", False))
        is_delinquent_on_federal_debt = input_decl.get("delinquentOnFederalDebt", borrower.get("presentlyDelinquentIndicator", False))
        is_obligated_on_other_loan = input_decl.get("obligatedOnOtherLoan", False)
        has_alimony_child_support = input_decl.get("alimonyChildSupport", False)
        has_borrowed_down_payment = input_decl.get("borrowedDownPayment", borrower.get("undisclosedBorrowedFundsIndicator", False))
        is_co_signer_on_other_debt = input_decl.get("coSignerOnOtherDebt", borrower.get("undisclosedComakerOfNoteIndicator", False))
        ownership_interest = input_decl.get("ownershipInterestInPropertyLast3Years", borrower.get("homeownerPastThreeYears", False))

        # --- HMDA mapping ---
        hmda_sex = None
        hmda_gender_details = borrower.get("hmdaGenderDetails") or {}
        if hmda_gender_details.get("hmdaGenderTypes"):
            # Use first non-empty value
            for val in hmda_gender_details["hmdaGenderTypes"]:
                if val:
                    hmda_sex = val
                    break
        elif borrower.get("hmdaSex"):
            hmda_sex = borrower["hmdaSex"] or None

        hmda_ethnicity = None
        hmda_eth_details = borrower.get("hmdaEthnicityDetails") or {}
        if hmda_eth_details.get("hmdaEthnicityTypes"):
            # Use first non-empty value
            for val in hmda_eth_details["hmdaEthnicityTypes"]:
                if val:
                    hmda_ethnicity = val
                    break
        elif borrower.get("hmdaEthnicity"):
            hmda_ethnicity = borrower["hmdaEthnicity"] or None

        # Ensure hmda_ethnicity is a list (API expects array)
        if hmda_ethnicity is not None and isinstance(hmda_ethnicity, str):
            hmda_ethnicity = [hmda_ethnicity]

        hmda_race = []
        hmda_race_details = borrower.get("hmdaRaceDetails") or {}
        if hmda_race_details.get("hmdaRaceTypes"):
            # Filter out empty strings
            hmda_race = [r for r in hmda_race_details["hmdaRaceTypes"] if r]
        elif borrower.get("hmdaRace"):
            # Filter out empty strings
            hmda_race = [r for r in borrower["hmdaRace"] if r]

        # --- Phone ---
        phone_cell = borrower.get("cellPhoneNumber", "")
        if not phone_cell:
            phone_numbers = borrower.get("phoneNumbers", [])
            for pn in phone_numbers:
                if pn.get("type") == "Mobile":
                    phone_cell = pn.get("number", "")
                    break

        # --- Email ---
        email = borrower.get("emailAddress", borrower.get("email"))

        # --- Borrower type ---
        borrower_type = "Primary" if borrower_index == 0 else "CoBorrower"

        # --- Citizenship ---
        citizenship_type = borrower.get("citizenshipType") or None
        is_us_citizen = borrower.get("isUsCitizen", citizenship_type == "USCitizen" if citizenship_type else True)
        is_permanent_resident = borrower.get("isPermanentResidentAlien", False)

        # --- Marital Status (handle empty string) ---
        marital_status = borrower.get("maritalStatus") or None

        # --- Date of Birth (handle empty string) ---
        date_of_birth = borrower.get("dateOfBirth") or None

        # --- Provenance ---
        created_at = borrower.get("createdAt", now)

        # Generate external_id for this borrower (used for cross-referencing)
        external_borrower_id = generate_stable_uuid(
            collection_id,
            "Borrower",
            borrower.get("id") or borrower.get("borrowerId") or borrower.get("ssnMasked") or borrower_index,
        )

        # --- Build and validate JazzBorrower using Pydantic ---
        validated_borrower = JazzBorrower(
            id=external_borrower_id,
            borrower_type=borrower_type,
            borrower_sequence=borrower_index + 1,
            first_name=first_name or None,
            middle_name=middle_name or None,
            last_name=last_name or None,
            ssn_masked=borrower.get("ssnMasked") or None,
            date_of_birth=date_of_birth,
            citizenship_status=citizenship_type,
            marital_status=marital_status,
            dependent_count=borrower.get("dependentCount", 0),
            dependent_ages=borrower.get("dependentAges", []),
            email=email or None,
            phone_cell=phone_cell or None,
            current_address=current_address,
            former_addresses=[],
            mailing_address_same_as_current=borrower.get("mailingAddressSameAsCurrent", True),
            is_first_time_homebuyer=borrower.get("firstTimeHomebuyerIndicator", False),
            will_occupy_as_primary_residence=borrower.get("intentToOccupy", True),
            ownership_interest_in_property_last_3_years=ownership_interest,
            has_outstanding_judgments=has_outstanding_judgments,
            has_declared_bankruptcy=has_declared_bankruptcy,
            has_property_foreclosed=has_property_foreclosed,
            is_party_to_lawsuit=is_party_to_lawsuit,
            has_conveyed_title_in_lieu=has_conveyed_title_in_lieu,
            has_preforeclosure_short_sale=has_preforeclosure_short_sale,
            is_delinquent_on_federal_debt=is_delinquent_on_federal_debt,
            is_obligated_on_other_loan=is_obligated_on_other_loan,
            has_alimony_child_support=has_alimony_child_support,
            is_co_signer_on_other_debt=is_co_signer_on_other_debt,
            has_borrowed_down_payment=has_borrowed_down_payment,
            is_us_citizen=is_us_citizen,
            is_permanent_resident_alien=is_permanent_resident,
            citizenship_country="US" if is_us_citizen else None,
            hmda_ethnicity=hmda_ethnicity,
            hmda_race=hmda_race,
            hmda_sex=hmda_sex,
            employment_ids=[],
            income_ids=[],
            asset_ids=[],
            liability_ids=[],
            reo_ids=[],
            provenance=build_provenance(created_at),
        )

        # Use mode='json' to serialize properly, exclude_none to skip None values
        json_value = validated_borrower.model_dump(mode="json", exclude_none=True)

        payload = {
            "entity_type": "Borrower",
            "ontology_id": BORROWER_ONTOLOGY_ID,
            "name": entity_name,
            "collection_id": collection_id,
            "json_value": json_value,
            "description": description,
        }

        return payload, external_borrower_id
