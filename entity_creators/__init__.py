"""
Entity Creators Package

This package contains payload builders for different Knowledge Hub entity types.
Each builder returns the payload that can be passed to the common EntityAPI.
"""

from .base import (
    # Ontology IDs
    LOAN_PROJECT_ONTOLOGY_ID,
    LOAN_CORE_ONTOLOGY_ID,
    BORROWER_ONTOLOGY_ID,
    ASSET_ONTOLOGY_ID,
    INCOME_SOURCE_ONTOLOGY_ID,
    LIABILITY_ONTOLOGY_ID,
    LOAN_DETAILS_ONTOLOGY_ID,
    LOAN_APPLICATION_ONTOLOGY_ID,
    SUBJECT_PROPERTY_ONTOLOGY_ID,
    EMPLOYMENT_RECORD_ONTOLOGY_ID,
    CREDIT_REPORT_ONTOLOGY_ID,
    # Utility functions
    generate_uuid,
    generate_stable_uuid,
    get_current_timestamp,
    build_provenance,
)

from .loan_project import LoanProjectPayloadBuilder
from .loan_core import LoanCorePayloadBuilder
from .borrower import BorrowerPayloadBuilder
from .asset import AssetPayloadBuilder
from .income import IncomePayloadBuilder
from .liability import LiabilityPayloadBuilder
from .subject_property import SubjectPropertyPayloadBuilder
from .employment import EmploymentPayloadBuilder
from .credit_report import CreditReportPayloadBuilder
from .loan_application import LoanApplicationPayloadBuilder
from .loan_details import LoanDetailsPayloadBuilder

__all__ = [
    # Ontology IDs
    "LOAN_PROJECT_ONTOLOGY_ID",
    "LOAN_CORE_ONTOLOGY_ID",
    "BORROWER_ONTOLOGY_ID",
    "ASSET_ONTOLOGY_ID",
    "INCOME_SOURCE_ONTOLOGY_ID",
    "LIABILITY_ONTOLOGY_ID",
    "LOAN_DETAILS_ONTOLOGY_ID",
    "LOAN_APPLICATION_ONTOLOGY_ID",
    "SUBJECT_PROPERTY_ONTOLOGY_ID",
    "EMPLOYMENT_RECORD_ONTOLOGY_ID",
    "CREDIT_REPORT_ONTOLOGY_ID",
    # Utility functions
    "generate_uuid",
    "generate_stable_uuid",
    "get_current_timestamp",
    "build_provenance",
    # Payload builders
    "LoanProjectPayloadBuilder",
    "LoanCorePayloadBuilder",
    "BorrowerPayloadBuilder",
    "AssetPayloadBuilder",
    "IncomePayloadBuilder",
    "LiabilityPayloadBuilder",
    "SubjectPropertyPayloadBuilder",
    "EmploymentPayloadBuilder",
    "CreditReportPayloadBuilder",
    "LoanApplicationPayloadBuilder",
    "LoanDetailsPayloadBuilder",
]
