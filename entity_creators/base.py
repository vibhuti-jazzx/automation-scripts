"""
Common utilities and constants for entity payload builders.
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

# =============================================================================
# Ontology IDs
# =============================================================================

# The main creator resolves these by ontology name and overwrites the payload
# values. Environment variables keep direct builder use instance-neutral too.
POC_MORTGAGE_ONTOLOGY_ID = os.getenv("JAZZX_MORTGAGE_ONTOLOGY_ID", "")
LOAN_PROJECT_ONTOLOGY_ID = os.getenv("JAZZX_LOAN_PROJECT_ONTOLOGY_ID", "")
LOAN_CORE_ONTOLOGY_ID = POC_MORTGAGE_ONTOLOGY_ID
BORROWER_ONTOLOGY_ID = POC_MORTGAGE_ONTOLOGY_ID
ASSET_ONTOLOGY_ID = POC_MORTGAGE_ONTOLOGY_ID
INCOME_SOURCE_ONTOLOGY_ID = POC_MORTGAGE_ONTOLOGY_ID
LIABILITY_ONTOLOGY_ID = POC_MORTGAGE_ONTOLOGY_ID
LOAN_APPLICATION_ONTOLOGY_ID = POC_MORTGAGE_ONTOLOGY_ID
SUBJECT_PROPERTY_ONTOLOGY_ID = POC_MORTGAGE_ONTOLOGY_ID
EMPLOYMENT_RECORD_ONTOLOGY_ID = POC_MORTGAGE_ONTOLOGY_ID
CREDIT_REPORT_ONTOLOGY_ID = POC_MORTGAGE_ONTOLOGY_ID
LOAN_DETAILS_ONTOLOGY_ID = os.getenv("JAZZX_LOAN_DETAILS_ONTOLOGY_ID", "")


# =============================================================================
# UUID Generation Helpers
# =============================================================================


def generate_uuid() -> str:
    """Generate a random UUID string."""
    return str(uuid.uuid4())


def generate_stable_uuid(*parts: object) -> str:
    """Generate a repeatable UUID for the same logical entity identity."""
    identity = "\x1f".join(str(part) for part in parts)
    if not identity:
        raise ValueError("at least one identity component is required")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def get_current_timestamp() -> str:
    """Get current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_provenance(created_at: str = None) -> Dict[str, Any]:
    """Build a standard provenance object.

    Args:
        created_at: Optional timestamp. If not provided, uses current time.

    Returns:
        A provenance dictionary.
    """
    if not created_at:
        created_at = get_current_timestamp()
    return {
        "source": "API",
        "created_at": created_at,
        "source_type": "LOS_API_SYNC",
        "source_system": "JAZZX_INTERNAL",
        "update_history": [],
    }
