"""
SubjectProperty entity payload builder.
"""

import logging
from typing import Any, Dict, Literal, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError, field_validator

from .base import (
    SUBJECT_PROPERTY_ONTOLOGY_ID,
    build_provenance,
    generate_stable_uuid,
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
# Address Model - Reusable address component
# ============================================================================


class Address(BaseModel):
    """Standard US address format."""

    street_line_1: Optional[str] = Field(None, max_length=255)
    street_line_2: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, description="US State code")
    postal_code: Optional[str] = Field(None)
    county: Optional[str] = Field(None, max_length=100)
    country: str = Field(default="US")


# ============================================================================
# Property Type Literals - Matches MISMO SubjectProperty ontology schema
# ============================================================================

OccupancyType = Literal["PrimaryResidence", "SecondHome", "Investment"]

PropertyType = Literal[
    "SingleFamily",
    "Condominium",
    "Townhouse",
    "Cooperative",
    "TwoToFourUnit",
    "ManufacturedSingleWide",
    "ManufacturedDoubleWide",
    "ManufacturedMultiWide",
    "PUD",
    "Modular",
    "MixedUse",
    "DetachedCondominium",
    "HighRiseCondominium",
]

ConstructionType = Literal[
    "Existing",
    "NewConstruction",
    "ConstructionToPermanent",
    "Proposed",
]

ManufacturedWidthType = Literal["SingleWide", "DoubleWide", "MultiWide"]

CondoProjectClassification = Literal[
    "Established",
    "NewProject",
    "TwoToFourUnitProject",
    "DetachedCondo",
    "Manufactured",
    "NonWarrantable",
]

TitleMannerHeld = Literal[
    "JointTenants",
    "TenantsInCommon",
    "CommunityProperty",
    "SoleOwnership",
    "Trust",
    "LLC",
    "Corporation",
    "Partnership",
    "LifeEstate",
]


# ============================================================================
# JazzSubjectProperty Model - MISMO SubjectProperty entity ontology schema
# ============================================================================


class JazzSubjectProperty(BaseModel):
    """
    Jazz SubjectProperty entity with automatic transformation from Vesta.

    This uses Pydantic's validation and transformation capabilities
    to map fields from Vesta SubjectProperty to Jazz's internal format.
    Follows the MISMO SubjectProperty entity ontology schema.
    """

    # Required fields
    id: str
    occupancy_type: OccupancyType

    # Property address
    address: Optional[Address] = None

    # Property characteristics
    property_type: Optional[PropertyType] = None
    number_of_units: Optional[int] = Field(None, ge=1, le=4)
    year_built: Optional[int] = Field(None, ge=1600, le=2100)
    legal_description: Optional[str] = None
    apn: Optional[str] = Field(None, description="Assessor Parcel Number")

    # Size information
    lot_size_acres: Optional[float] = Field(None, ge=0)
    lot_size_sqft: Optional[float] = Field(None, ge=0)
    living_area_sqft: Optional[float] = Field(None, ge=0)
    bedrooms: Optional[int] = Field(None, ge=0)
    bathrooms: Optional[float] = Field(None, ge=0)

    # Construction details
    construction_type: Optional[ConstructionType] = None

    # Manufactured home details
    is_manufactured: Optional[bool] = None
    manufactured_width_type: Optional[ManufacturedWidthType] = None

    # PUD/Condo details
    is_pud: Optional[bool] = None
    is_condo: Optional[bool] = None
    condo_project_classification: Optional[CondoProjectClassification] = None
    hoa_monthly_usd: Optional[float] = Field(None, ge=0)

    # Value information
    purchase_price_usd: Optional[float] = Field(None, ge=0)
    estimated_value_usd: Optional[float] = Field(None, ge=0)
    appraised_value_usd: Optional[float] = Field(None, ge=0)
    appraisal_date: Optional[str] = Field(None, description="Date in YYYY-MM-DD format")
    improvements_cost_usd: Optional[float] = Field(None, ge=0)
    land_value_usd: Optional[float] = Field(None, ge=0)

    # Mixed use details
    is_mixed_use: Optional[bool] = None
    mixed_use_commercial_percent: Optional[float] = Field(None, ge=0, le=100)

    # Flood zone information
    flood_zone: Optional[str] = Field(None, max_length=10)
    is_in_special_flood_hazard_area: Optional[bool] = None

    # Title information
    title_manner_held: Optional[TitleMannerHeld] = None

    # Provenance (optional, typically added by system)
    provenance: Optional[dict] = None

    # Validator to round percentage fields
    @field_validator("mixed_use_commercial_percent", mode="before")
    @classmethod
    def round_percentage(cls, v):
        return _round_percentage(v)


class SubjectPropertyPayloadBuilder:
    """Builds payload for SubjectProperty entity."""

    @staticmethod
    def build(
        collection_id: str,
        input_data: Dict[str, Any],
        description: str = "Subject property entity",
    ) -> Optional[Tuple[Dict[str, Any], str]]:
        """Build the payload for a SubjectProperty entity.

        Args:
            collection_id: The collection ID from project creation.
            input_data: The full input JSON data.
            description: Description for the entity.

        Returns:
            A tuple of (payload dict, external_property_id), or None if no subject_property data.

        Raises:
            ValidationError: If the subject property data fails Pydantic validation.
        """
        subject_property = input_data.get("subject_property") or {}
        if not subject_property:
            return None

        now = get_current_timestamp()
        external_property_id = generate_stable_uuid(
            collection_id, "SubjectProperty"
        )

        # --- Address mapping ---
        input_addr = subject_property.get("address") or {}
        address_data = {
            "city": input_addr.get("city") or None,
            "state": input_addr.get("stateCode", input_addr.get("state")) or None,
            "county": input_addr.get("county") or None,
            "country": "US",
            "postal_code": input_addr.get("zipCode", input_addr.get("zip")) or None,
            "street_line_1": input_addr.get("line", input_addr.get("fullStreetAddress", input_addr.get("street"))) or None,
        }

        # Extract city and state for entity name
        city = address_data.get("city") or "Unknown"
        state = address_data.get("state") or ""
        entity_name = f"SubjectProperty-{city}-{state}" if state else f"SubjectProperty-{city}"

        # --- Build and validate JazzSubjectProperty using Pydantic ---
        try:
            # Create validated Address object
            validated_address = Address(**address_data)

            validated_subject_property = JazzSubjectProperty(
                id=external_property_id,
                occupancy_type=subject_property.get("intendedUsageType", subject_property.get("occupancyType", "PrimaryResidence")),
                address=validated_address,
                property_type=subject_property.get("propertyType", "SingleFamily"),
                number_of_units=subject_property.get("numberOfUnits", 1),
                year_built=subject_property.get("yearBuilt"),
                legal_description=subject_property.get("legalDescription"),
                apn=subject_property.get("apn"),
                lot_size_acres=subject_property.get("lotSizeAcres"),
                lot_size_sqft=subject_property.get("lotSizeSqft"),
                living_area_sqft=subject_property.get("livingAreaSqft"),
                bedrooms=subject_property.get("bedrooms"),
                bathrooms=subject_property.get("bathrooms"),
                construction_type=subject_property.get("constructionType", "Existing"),
                is_manufactured=subject_property.get("isManufactured", False),
                manufactured_width_type=subject_property.get("manufacturedWidthType"),
                is_pud=subject_property.get("isPud", False),
                is_condo=subject_property.get("isCondo", False),
                condo_project_classification=subject_property.get("condoProjectClassification"),
                hoa_monthly_usd=float(subject_property.get("hoaMonthly", 0) or 0),
                purchase_price_usd=float(subject_property.get("purchasePrice", 0) or 0),
                estimated_value_usd=float(subject_property.get("estimatedValueAmount", subject_property.get("estimatedValue", 0)) or 0),
                appraised_value_usd=float(subject_property.get("appraisedValue", subject_property.get("appraisalValue", 0)) or 0),
                appraisal_date=subject_property.get("appraisalDate"),
                improvements_cost_usd=float(subject_property.get("improvementsCost", 0) or 0) if subject_property.get("improvementsCost") else None,
                land_value_usd=float(subject_property.get("landValue", 0) or 0) if subject_property.get("landValue") else None,
                is_mixed_use=subject_property.get("isMixedUse", False),
                mixed_use_commercial_percent=subject_property.get("mixedUseCommercialPercent"),
                flood_zone=subject_property.get("floodZone"),
                is_in_special_flood_hazard_area=subject_property.get("isInSpecialFloodHazardArea", False),
                title_manner_held=subject_property.get("titleMannerHeld"),
                provenance=build_provenance(),
            )

            # Use mode='json' to serialize properly, exclude_none to skip None values
            json_value = validated_subject_property.model_dump(mode="json", exclude_none=True)

        except ValidationError as ve:
            logger.error(f"SubjectProperty validation failed: {ve.errors()}")
            raise

        payload = {
            "entity_type": "SubjectProperty",
            "ontology_id": SUBJECT_PROPERTY_ONTOLOGY_ID,
            "name": entity_name,
            "collection_id": collection_id,
            "json_value": json_value,
            "description": description,
        }

        return payload, external_property_id
