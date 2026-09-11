"""
LoanProject entity payload builder.
"""

import logging
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from .base import (
    LOAN_PROJECT_ONTOLOGY_ID,
    get_current_timestamp,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Event Type Literals - Matches LoanProject ontology schema
# ============================================================================

EventName = Literal[
    "WEBHOOK_RECEIVED",
    "WORKSPACE_CREATED",
    "DOWNLOADING_FILES",
    "PROCESSING_FILES",
    "FILES_DOWNLOADED",
    "FILES_PROCESSED",
    "PARSING_DOCUMENTS",
    "DOCUMENTS_PARSED",
    "CLASSIFICATION_STARTED",
    "CLASSIFICATION_COMPLETED",
    "DUPLICATE_CHECK_STARTED",
    "DUPLICATE_CHECK_COMPLETED",
    "WORKFLOW_COMPLETED",
    "WORKFLOW_FAILED",
    "ERROR_OCCURRED",
]

LoanApplicationEventName = Literal[
    "LOAN_APPLICATION_CREATED",
    "LOAN_APPLICATION_CREATION_FAILED",
]

LoanDetailsEventName = Literal[
    "LOAN_DETAILS_CREATED",
    "LOAN_DETAILS_CREATION_FAILED",
]

ProjectStatus = Literal[
    "CREATED",
    "DOWNLOADING",
    "PROCESSING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]


# ============================================================================
# Nested Models - Event structures
# ============================================================================


class LoanProjectEvent(BaseModel):
    """Processing event that occurred for this loan project."""

    name: EventName
    timestamp: str = Field(..., description="ISO 8601 timestamp when the event occurred")
    message: Optional[str] = Field(None, description="Optional human-readable message providing additional context")


class LoanApplicationEvent(BaseModel):
    """Loan application creation details from LOS system."""

    name: LoanApplicationEventName
    timestamp: str = Field(..., description="ISO 8601 timestamp when the loan application was created")
    entity_id: Optional[str] = Field(None, description="Entity ID from the tool response")
    message: Optional[str] = Field(None, description="Human-readable message providing context")


class LoanDetailsEvent(BaseModel):
    """Loan details creation details from loan application entity."""

    name: LoanDetailsEventName
    timestamp: str = Field(..., description="ISO 8601 timestamp when the loan details were created")
    entity_id: Optional[str] = Field(None, description="Entity ID from the tool response")
    message: Optional[str] = Field(None, description="Human-readable message providing context")


# ============================================================================
# JazzLoanProject Model - LoanProject entity ontology schema
# ============================================================================


class JazzLoanProject(BaseModel):
    """
    Jazz LoanProject entity for tracking loan-level document processing workflows.

    This schema represents loan project metadata and processing events
    for tracking document workflow at the loan level.
    """

    # Required fields
    status: ProjectStatus
    external_loan_id: str = Field(..., description="Loan ID from the external LOS system (e.g., Encompass GUID)")
    collection_id: str = Field(..., description="Unique identifier for the collection associated with this loan project")
    events: List[LoanProjectEvent] = Field(default_factory=list, description="Array of processing events")
    created_at: str = Field(..., description="ISO 8601 timestamp when record was created")
    updated_at: str = Field(..., description="ISO 8601 timestamp when record was last updated")

    # Optional fields
    project_id: Optional[str] = Field(None, description="Internal unique identifier for the loan project")
    external_loan_number: Optional[str] = Field(None, description="Human-readable loan number from the external system")
    assigned_to: Optional[str] = Field(None, description="User or system identifier to whom/which this loan project is assigned")
    project_notepad_id: Optional[str] = Field(None, description="Optional identifier for the project notepad")
    loan_application: Optional[LoanApplicationEvent] = Field(None, description="Loan application creation details from LOS system")
    loan_details: Optional[LoanDetailsEvent] = Field(None, description="Loan details creation details from loan application entity")


class LoanProjectPayloadBuilder:
    """Builds payload for LoanProject entity."""

    @staticmethod
    def build(
        loan_id: str,
        collection_id: str,
        project_id: str,
        loan_number: str = "",
        description: str = "Loan project",
    ) -> Dict[str, Any]:
        """Build the payload for a LoanProject entity.

        Args:
            loan_id: The external loan ID (also used as the entity name).
            collection_id: The collection ID from project creation.
            project_id: The project ID from project creation.
            loan_number: The loan number from the input data.
            description: Description for the entity.

        Returns:
            The payload dictionary ready for the entity API.

        Raises:
            ValidationError: If the loan project data fails Pydantic validation.
        """
        now = get_current_timestamp()

        # --- Build and validate JazzLoanProject using Pydantic ---
        try:
            validated_loan_project = JazzLoanProject(
                status="CREATED",
                external_loan_id=loan_id,
                collection_id=collection_id,
                events=[],
                created_at=now,
                updated_at=now,
                project_id=project_id,
                external_loan_number=loan_number if loan_number else None,
            )

            # Use mode='json' to serialize properly, exclude_none to skip None values
            json_value = validated_loan_project.model_dump(mode="json", exclude_none=True)

        except ValidationError as ve:
            logger.error(f"LoanProject validation failed: {ve.errors()}")
            raise

        return {
            "entity_type": "LoanProject",
            "ontology_id": LOAN_PROJECT_ONTOLOGY_ID,
            "name": loan_number,
            "collection_id": collection_id,
            "json_value": json_value,
            "description": description,
        }
