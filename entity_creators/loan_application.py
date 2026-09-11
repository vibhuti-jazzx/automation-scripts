"""
LoanApplication entity payload builder.

The LoanApplication entity is a hub that links all other entities together
using BorrowerLinkedReferences.
"""

from typing import Any, Dict, List, Optional

from .base import (
    LOAN_APPLICATION_ONTOLOGY_ID,
    get_current_timestamp,
)


class LoanApplicationPayloadBuilder:
    """Builds payload for LoanApplication entity.

    The LoanApplication entity is a hub that links all other entities together
    using BorrowerLinkedReferences.
    """

    @staticmethod
    def build(
        loan_id: str,
        collection_id: str,
        loan_core_internal_id: str = None,
        borrower_refs: List[Dict[str, str]] = None,
        asset_refs: List[Dict[str, str]] = None,
        income_refs: List[Dict[str, str]] = None,
        liability_refs: List[Dict[str, str]] = None,
        employment_refs: List[Dict[str, str]] = None,
        credit_report_refs: List[Dict[str, str]] = None,
        description: str = "Loan application entity",
    ) -> Dict[str, Any]:
        """Build the payload for a LoanApplication entity.

        Args:
            loan_id: The external loan ID.
            collection_id: The collection ID from project creation.
            loan_core_internal_id: The internal ID of the LoanCore entity.
            borrower_refs: List of borrower references [{external_id, internal_id}].
            asset_refs: List of asset references [{borrower_id, external_id, internal_id}].
            income_refs: List of income references [{borrower_id, external_id, internal_id}].
            liability_refs: List of liability references [{borrower_id, external_id, internal_id}].
            employment_refs: List of employment references [{borrower_id, external_id, internal_id}].
            credit_report_refs: List of credit report references [{borrower_id, external_id, internal_id}].
            description: Description for the entity.

        Returns:
            The payload dictionary ready for the entity API.
        """
        now = get_current_timestamp()

        entity_name = f"LoanApplication-{loan_id[:8]}"

        # --- Build loan_core reference ---
        loan_core_ref = {
            "external_id": loan_id,
            "internal_id": loan_core_internal_id,
        }

        # --- Build json_value ---
        json_value = {
            "loan_core": loan_core_ref,
            "borrowers": borrower_refs or [],
            "assets": asset_refs or [],
            "liabilities": liability_refs or [],
            "income_sources": income_refs or [],
            "employment_records": employment_refs or [],
            "credit_reports": credit_report_refs or [],
            "created_at": now,
            "updated_at": now,
        }

        return {
            "entity_type": "LoanApplication",
            "ontology_id": LOAN_APPLICATION_ONTOLOGY_ID,
            "name": entity_name,
            "collection_id": collection_id,
            "json_value": json_value,
            "description": description,
        }
