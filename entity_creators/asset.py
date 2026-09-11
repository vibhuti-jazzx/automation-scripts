"""
Asset entity payload builder.
"""

import logging
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from .base import (
    ASSET_ONTOLOGY_ID,
    build_provenance,
    generate_stable_uuid,
    get_current_timestamp,
)

logger = logging.getLogger(__name__)


# ============================================================================
# US State Code Type
# ============================================================================

USStateCode = Literal[
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL",
    "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
    "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "PR",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "VI", "WA",
    "WV", "WI", "WY", "GU", "AS", "MP",
]


# ============================================================================
# Address Model - Matches ontology $defs/Address
# ============================================================================


class Address(BaseModel):
    """Standard US address format matching ontology Address schema."""

    street_line_1: Optional[str] = Field(None, max_length=255)
    street_line_2: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[USStateCode] = None
    postal_code: Optional[str] = None
    county: Optional[str] = Field(None, max_length=100)
    country: str = Field(default="US")


# ============================================================================
# Asset Type Literal - Matches MISMO Asset entity ontology schema
# ============================================================================

AssetType = Literal[
    "CheckingAccount",
    "SavingsAccount",
    "MoneyMarket",
    "CertificateOfDeposit",
    "MutualFund",
    "Stock",
    "Bond",
    "RetirementAccount",
    "IRA",
    "401k",
    "403b",
    "PensionFund",
    "TrustFund",
    "LifeInsuranceCashValue",
    "StockOptions",
    "BridgeLoanProceeds",
    "IndividualDevelopmentAccount",
    "CashOnHand",
    "GiftFunds",
    "Grant",
    "RealEstateEquity",
    "SecuredBorrowedFunds",
    "UnsecuredBorrowedFunds",
    "SaleOfChattel",
    "TradeEquity",
    "SweatEquity",
    "CashDepositOnSalesContract",
    "RelocationFunds",
    "EmployerAssistedHousing",
    "LeasePurchaseFund",
    "LotEquity",
    "RentWithOptionToPurchase",
    "OtherLiquidAssets",
    "OtherNonLiquidAssets",
    "ProceedsFromSaleOfHome",
    "ProceedsFromSecuredLoan",
    "ProceedsFromUnsecuredLoan",
    "EarnestMoneyDeposit",
]


# ============================================================================
# JazzAsset Model - MISMO Asset entity ontology schema
# ============================================================================


class JazzAsset(BaseModel):
    """
    Jazz Asset entity with validation.

    This uses Pydantic's validation and transformation capabilities
    to validate asset data before sending to the API.
    Follows the MISMO Asset entity ontology schema.
    """

    # Required fields
    id: str
    borrower_id: str
    asset_type: AssetType

    # Institution details
    institution_name: Optional[str] = Field(None, max_length=255)
    institution_address: Optional[Address] = None

    # Account details
    account_number_masked: Optional[str] = Field(None, max_length=20)
    current_balance_usd: Optional[float] = Field(None, ge=0)

    # Asset classification
    is_liquid: Optional[bool] = None

    # Gift details
    is_gift: Optional[bool] = None
    gift_donor_name: Optional[str] = None
    gift_donor_relationship: Optional[str] = None

    # Closing usage
    will_be_used_for_closing: Optional[bool] = None

    # Provenance (optional, typically added by system)
    provenance: Optional[dict] = None


class AssetPayloadBuilder:
    """Builds payload for Asset entities."""

    @staticmethod
    def build_all(
        collection_id: str,
        input_data: Dict[str, Any],
        borrower_mappings: List[Dict[str, str]] = None,
        description: str = "Asset entity",
    ) -> List[Tuple[Dict[str, Any], str, str]]:
        """Build payloads for all assets across all borrowers.

        Args:
            collection_id: The collection ID from project creation.
            input_data: The full input JSON data.
            borrower_mappings: List of dicts with 'internal_id' and 'external_id' keys.
            description: Description for the entities.

        Returns:
            A list of tuples (payload, external_id, borrower_id) for each asset.
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

            for asset_idx, asset in enumerate(borrower.get("assets", [])):
                try:
                    payload, external_id = AssetPayloadBuilder.build_single(
                        collection_id=collection_id,
                        asset=asset,
                        borrower_internal_id=borrower_internal_id,
                        description=description,
                        external_asset_id=generate_stable_uuid(
                            collection_id,
                            "Asset",
                            borrower_internal_id,
                            asset.get("id") or asset.get("assetId") or asset.get("accountNumber") or asset_idx,
                        ),
                    )
                    results.append((payload, external_id, borrower_internal_id))
                except ValidationError as ve:
                    logger.error(
                        f"Asset validation failed for borrower {borrower_internal_id}: {ve.errors()}"
                    )
                    raise
        return results

    @staticmethod
    def build_single(
        collection_id: str,
        asset: Dict[str, Any],
        borrower_internal_id: str,
        description: str = "Asset entity",
        external_asset_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Build the payload for a single Asset entity.

        Args:
            collection_id: The collection ID from project creation.
            asset: The asset data from input.
            borrower_internal_id: The internal ID of the borrower.
            description: Description for the entity.

        Returns:
            A tuple of (payload dict, external_asset_id).

        Raises:
            ValidationError: If the asset data fails Pydantic validation.
        """
        now = get_current_timestamp()
        external_asset_id = external_asset_id or generate_stable_uuid(
            collection_id,
            "Asset",
            borrower_internal_id,
            asset.get("id") or asset.get("assetId") or asset.get("accountNumber") or asset.get("assetType", "Unknown"),
        )
        asset_type = asset.get("assetType", "Unknown")
        entity_name = f"Asset-{asset_type}-{external_asset_id[:8]}"

        # --- Institution address mapping (camelCase → snake_case) ---
        input_inst_addr = asset.get("institutionAddress") or {}
        institution_address = None
        if input_inst_addr:
            # Use "US" as default if country is missing or empty
            country = input_inst_addr.get("country") or "US"
            institution_address = Address(
                city=input_inst_addr.get("city"),
                state=input_inst_addr.get("state"),
                country=country,
                postal_code=input_inst_addr.get("postalCode"),
                street_line_1=input_inst_addr.get("streetLine1"),
                street_line_2=input_inst_addr.get("streetLine2"),
                county=input_inst_addr.get("county"),
            )

        # --- Build and validate JazzAsset using Pydantic ---
        validated_asset = JazzAsset(
            id=external_asset_id,
            borrower_id=borrower_internal_id,
            asset_type=asset_type,
            is_gift=asset.get("isGift", False),
            is_liquid=asset.get("isLiquid", True),
            provenance=build_provenance(),
            institution_name=asset.get("institutionName"),
            institution_address=institution_address,
            current_balance_usd=asset.get("currentBalance", asset.get("assetValue", 0)),
            account_number_masked=asset.get("accountNumber", asset.get("accountIdentifier")),
            will_be_used_for_closing=asset.get("willBeUsedForClosing", False),
            gift_donor_name=asset.get("giftDonorName"),
            gift_donor_relationship=asset.get("giftDonorRelationship"),
        )

        # Use mode='json' to serialize properly, exclude_none to skip None values
        json_value = validated_asset.model_dump(mode="json", exclude_none=True)

        payload = {
            "entity_type": "Asset",
            "ontology_id": ASSET_ONTOLOGY_ID,
            "name": entity_name,
            "collection_id": collection_id,
            "json_value": json_value,
            "description": description,
        }

        return payload, external_asset_id
