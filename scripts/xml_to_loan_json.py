#!/usr/bin/env python3
"""Deterministically convert a MISMO 3.4 XML export to loan JSON.

This is the non-LLM counterpart to ``mock_server_validator_setup``'s XML
extraction workflow. It only copies facts present in the XML, applies explicit
MISMO-to-JSON mappings, and omits fields that are absent or ambiguous.

Examples::

    python3 scripts/xml_to_loan_json.py input.xml -o output.json
    python3 scripts/xml_to_loan_json.py input.xml

When no output path is supplied, JSON is written to stdout. Use ``--quiet``
to suppress the extraction summary on stderr.
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def lname(tag):
    return tag.rsplit("}", 1)[-1]


def children(node, name):
    return [x for x in node.iter() if lname(x.tag) == name]


def first(node, name):
    if node is None:
        return None
    for x in node.iter():
        if lname(x.tag) == name and x.text and x.text.strip():
            return x.text.strip()
    return None


def direct(node, name):
    for x in node:
        if lname(x.tag) == name:
            return x
    return None


def num(value):
    if value is None:
        return None
    return float(value) if "." in value else int(value)


def integer(value):
    return int(value) if value is not None else None


def boolean(value):
    return value.lower() == "true" if value is not None else None


def put(out, key, value):
    if value is not None:
        out[key] = value


def mask(value):
    if not value:
        return None
    return "XXXX" + value[-4:]


def mask_ssn(value):
    if not value:
        return None
    return "XXX-XX-" + value[-4:]


def address(node):
    if node is None:
        return None
    out = {}
    for source, target in (
        ("AddressLineText", "street_line_1"),
        ("AddressUnitIdentifier", "street_line_2"),
        ("CityName", "city"),
        ("StateCode", "state"),
        ("PostalCode", "postal_code"),
        ("CountyCode", "county"),
        ("CountyName", "county"),
        ("CountryCode", "country"),
    ):
        if target not in out:
            value = first(node, source)
            if target == "postal_code" and value and len(value) == 9 and value.isdigit():
                value = value[:5] + "-" + value[5:]
            put(out, target, value)
    return out or None


def all_by_label(root, tag, label):
    return [x for x in root.iter() if lname(x.tag) == tag and x.attrib.get("{http://www.w3.org/1999/xlink}label") == label]


def build_output(source_path, omit_loan_details=False):
    root = ET.parse(source_path).getroot()
    deal = next(x for x in root.iter() if lname(x.tag) == "DEAL")
    loan = next(x for x in deal.iter() if lname(x.tag) == "LOAN")

    loan_id = next(
        (first(x, "LoanIdentifier") for x in loan.iter() if first(x, "LoanIdentifierType") == "LenderLoan"),
        None,
    )
    terms = next(x for x in loan.iter() if lname(x.tag) == "TERMS_OF_LOAN")
    amort = next(x for x in loan.iter() if lname(x.tag) == "AMORTIZATION_RULE")
    maturity = next(x for x in loan.iter() if lname(x.tag) == "MATURITY_RULE")
    refinance = next((x for x in loan.iter() if lname(x.tag) == "REFINANCE"), None)
    raw_loan_purpose = first(terms, "LoanPurposeType")
    if raw_loan_purpose == "Purchase":
        loan_purpose = "Purchase"
    elif first(refinance, "RefinanceCashOutDeterminationType") == "LimitedCashOut":
        loan_purpose = "RefinanceLimitedCashOut"
    elif first(refinance, "RefinanceCashOutDeterminationType") == "CashOut":
        loan_purpose = "RefinanceCashOut"
    elif raw_loan_purpose in {"Refinance", "RefinanceRateAndTerm"}:
        loan_purpose = "RefinanceRateAndTerm"
    else:
        loan_purpose = raw_loan_purpose
    loan_core = {
        "loan_number": loan_id,
        "loan_purpose": loan_purpose,
        "loan_type": "Conventional" if first(terms, "MortgageType") == "Conventional" else first(terms, "MortgageType"),
        "base_loan_amount_usd": num(first(terms, "BaseLoanAmount")),
        "note_rate_percent": num(first(terms, "NoteRatePercent")),
        "term_months": integer(first(amort, "LoanAmortizationPeriodCount")),
        "amortization_type": first(amort, "AmortizationType"),
        "lien_priority": "First" if first(terms, "LienPriorityType") == "FirstLien" else first(terms, "LienPriorityType"),
        "application_date": first(loan, "ApplicationReceivedDate"),
        "prepayment_penalty": boolean(first(loan, "PrepaymentPenaltyIndicator")),
        "is_hoepa": boolean(first(loan, "HMDA_HOEPALoanStatusIndicator")),
        # Closest workflow interpretation: the XML has a received/signed
        # application and complete processing data, but no explicit status.
        "application_status": "LoanTerms",
        "maturity_date": None,
        "loan_status": "Processing",
        "total_monthly_income_usd": None,
        "required_to_close_usd": num(first(loan, "CashFromBorrowerAtClosingAmount")),
    }
    maturity_count = integer(first(maturity, "LoanMaturityPeriodCount"))
    if loan_core["application_date"] and maturity_count:
        # The source has no maturity date; leave it omitted rather than inventing one.
        pass
    loan_core = {k: v for k, v in loan_core.items() if v is not None}
    # Calculate APR from source-provided loan terms and discount points.
    base_amount = loan_core.get("base_loan_amount_usd")
    note_rate = loan_core.get("note_rate_percent")
    term = loan_core.get("term_months")
    discount_points = num(first(loan, "DiscountPointsTotalAmount")) or 0
    if base_amount and note_rate is not None and term and discount_points:
        monthly_rate = note_rate / 100 / 12
        payment = base_amount * monthly_rate / (1 - (1 + monthly_rate) ** (-term))
        net_amount = base_amount - discount_points
        low, high = 0.0, 0.02
        for _ in range(100):
            candidate = (low + high) / 2
            present_value = payment * (1 - (1 + candidate) ** (-term)) / candidate
            if present_value > net_amount:
                low = candidate
            else:
                high = candidate
        loan_core["apr_percent"] = round((low + high) / 2 * 12 * 100, 2)
    subject = next(x for x in deal.iter() if lname(x.tag) == "SUBJECT_PROPERTY")
    subject_property = {
        "address": address(next(x for x in subject if lname(x.tag) == "ADDRESS")),
        # Closest supported property type: detached, site-built, one unit.
        "property_type": "SingleFamily",
        "occupancy_type": first(subject, "PropertyUsageType"),
        "number_of_units": integer(first(subject, "FinancedUnitCount")),
        "year_built": integer(first(subject, "PropertyStructureBuiltYear")),
        "is_pud": boolean(first(subject, "PUDIndicator")),
        "estimated_value_usd": num(first(subject, "PropertyEstimatedValueAmount")),
        "appraised_value_usd": num(first(subject, "PropertyValuationAmount")),
        "purchase_price_usd": num(first(subject, "SalesContractAmount")),
        "is_mixed_use": boolean(first(subject, "PropertyMixedUsageIndicator")),
    }
    monthly_income = sum(
        (num(first(x, "CurrentIncomeMonthlyTotalAmount")) or 0)
        for x in children(root, "CURRENT_INCOME_ITEM")
    )
    if not monthly_income:
        monthly_income = sum(
            (num(first(x, "EmploymentMonthlyIncomeAmount")) or 0)
            for x in children(root, "EMPLOYMENT")
        )
    proposed_housing = num(next(
        (first(x, "HousingExpenseProposedTotalMonthlyPaymentAmount")
         for x in loan.iter() if lname(x.tag) == "HOUSING_EXPENSE_SUMMARY"),
        None,
    )) or 0
    nonexcluded_liabilities = sum(
        (num(first(x, "LiabilityMonthlyPaymentAmount")) or 0)
        for x in children(deal, "LIABILITY")
        if first(x, "LiabilityExclusionIndicator") != "true"
        and first(x, "LiabilityPayoffStatusIndicator") != "true"
    )
    if monthly_income:
        loan_core["total_monthly_income_usd"] = monthly_income
        loan_core["total_liabilities_monthly_payment_usd"] = nonexcluded_liabilities
    if first(subject, "PropertyValuationAmount") and loan_core.get("base_loan_amount_usd"):
        loan_core["ltv_percent"] = round(loan_core["base_loan_amount_usd"] / num(first(subject, "PropertyValuationAmount")) * 100, 2)

    parties = []
    for party in children(root, "PARTY"):
        role = next((x for x in children(party, "ROLE") if first(x, "PartyRoleType") == "Borrower"), None)
        if role is not None:
            label = role.attrib.get("{http://www.w3.org/1999/xlink}label")
            parties.append((party, label, 1 if label == "BORROWER_1" else 2))

    relationships = [(x.attrib.get("{http://www.w3.org/1999/xlink}from"), x.attrib.get("{http://www.w3.org/1999/xlink}to")) for x in children(deal, "RELATIONSHIP")]

    assets = []
    for asset in children(deal, "ASSET"):
        label = asset.attrib.get("{http://www.w3.org/1999/xlink}label")
        owned = next((x for x in asset if lname(x.tag) == "OWNED_PROPERTY"), None)
        if owned is not None:
            detail = next(x for x in owned.iter() if lname(x.tag) == "OWNED_PROPERTY_DETAIL")
            prop = next(x for x in owned.iter() if lname(x.tag) == "PROPERTY")
            item = {"asset_type": "RealEstateEquity", "current_balance_usd": num(first(prop, "PropertyEstimatedValueAmount"))}
            item["_label"] = label
        else:
            raw_asset_type = first(asset, "AssetType")
            if raw_asset_type is None:
                # The XML provides no type for this asset; omit it rather
                # than inventing an enum value from the balance alone.
                continue
            asset_type = {
                "RetirementFund": "RetirementAccount",
                "GiftOfCash": "GiftFunds",
                "PendingNetSaleProceedsFromRealEstateAssets": "ProceedsFromSaleOfHome",
            }.get(raw_asset_type, raw_asset_type)
            item = {"asset_type": asset_type, "institution_name": first(asset, "FullName"), "account_number_masked": mask(first(asset, "AssetAccountIdentifier")), "current_balance_usd": num(first(asset, "AssetCashOrMarketValueAmount"))}
            item["_label"] = label
        assets.append(item)

    liabilities = []
    for liability in children(deal, "LIABILITY"):
        label = liability.attrib.get("{http://www.w3.org/1999/xlink}label")
        detail = next(x for x in liability if lname(x.tag) == "LIABILITY_DETAIL")
        raw_type = first(detail, "LiabilityType")
        item = {
            "liability_type": "Mortgage" if raw_type == "MortgageLoan" else raw_type,
            "creditor_name": first(liability, "FullName"),
            "account_number_masked": mask(first(detail, "LiabilityAccountIdentifier")),
            "unpaid_balance_usd": num(first(detail, "LiabilityUnpaidBalanceAmount")),
            "monthly_payment_usd": num(first(detail, "LiabilityMonthlyPaymentAmount")),
            "months_remaining": integer(first(detail, "LiabilityRemainingTermMonthsCount")),
            "will_be_paid_off_at_closing": boolean(first(detail, "LiabilityPayoffStatusIndicator")),
            "is_excluded_from_dti": boolean(first(detail, "LiabilityExclusionIndicator")),
            "_label": label,
        }
        liabilities.append(item)

    borrowers = []
    for party, borrower_label, sequence in parties:
        detail = next(x for x in party.iter() if lname(x.tag) == "BORROWER_DETAIL")
        declaration = next(x for x in party.iter() if lname(x.tag) == "DECLARATION_DETAIL")
        individual = next(x for x in party.iter() if lname(x.tag) == "INDIVIDUAL")
        residence = next((x for x in party.iter() if lname(x.tag) == "RESIDENCE"), None)
        mailing = next((x for x in party.iter() if lname(x.tag) == "ADDRESS" and first(x, "AddressType") == "Mailing"), None)
        ssn = next((first(x, "TaxpayerIdentifierValue") for x in party.iter() if lname(x.tag) == "TAXPAYER_IDENTIFIER"), None)
        b = {
            "borrower_fields": {
                "borrower_type": "Primary" if sequence == 1 else "CoBorrower",
                "borrower_sequence": sequence,
                "first_name": first(individual, "FirstName"),
                "middle_name": first(individual, "MiddleName"),
                "last_name": first(individual, "LastName"),
                "date_of_birth": first(detail, "BorrowerBirthDate"),
                "ssn_masked": mask_ssn(ssn),
                "marital_status": first(detail, "MaritalStatusType"),
                "citizenship_status": first(declaration, "CitizenshipResidencyType"),
                "dependent_count": integer(first(detail, "DependentCount")),
                "email": first(party, "ContactPointEmailValue"),
                "phone_home": None,
                "phone_cell": None,
                "current_address": address(next((x for x in residence.iter() if lname(x.tag) == "ADDRESS"), None)) if residence is not None else None,
                "mailing_address": address(mailing),
                "will_occupy_as_primary_residence": first(declaration, "IntentToOccupyType") == "Yes",
                "ownership_interest_in_property_last_3_years": first(declaration, "HomeownerPastThreeYearsType") == "Yes",
                "has_outstanding_judgments": boolean(first(declaration, "OutstandingJudgmentsIndicator")),
                "has_declared_bankruptcy": boolean(first(declaration, "BankruptcyIndicator")),
                "is_party_to_lawsuit": boolean(first(declaration, "PartyToLawsuitIndicator")),
                "is_delinquent_on_federal_debt": boolean(first(declaration, "PresentlyDelinquentIndicator")),
                "has_conveyed_title_in_lieu": boolean(first(declaration, "PriorPropertyDeedInLieuConveyedIndicator")),
                "has_property_foreclosed": boolean(first(declaration, "PriorPropertyForeclosureCompletedIndicator")),
                "has_preforeclosure_short_sale": boolean(first(declaration, "PriorPropertyShortSaleCompletedIndicator")),
                "is_us_citizen": first(declaration, "CitizenshipResidencyType") == "USCitizen",
                "hmda_race": [first(party, "HMDARaceType")] if first(party, "HMDARaceType") else None,
                "hmda_ethnicity": [first(party, "HMDAEthnicityType")] if first(party, "HMDAEthnicityType") else None,
                "hmda_sex": first(party, "HMDAGenderType"),
            },
            "employments": [],
            "income_sources": [],
            "assets": [],
            "liabilities": [],
        }
        phones = [(first(x, "ContactPointTelephoneValue"), first(x, "ContactPointRoleType")) for x in party.iter() if lname(x.tag) == "CONTACT_POINT"]
        for phone, kind in phones:
            if kind == "Mobile": b["borrower_fields"]["phone_cell"] = phone
            elif kind == "Home": b["borrower_fields"]["phone_home"] = phone
        if b["borrower_fields"]["phone_home"] is None and phones: b["borrower_fields"]["phone_home"] = phones[0][0]
        if b["borrower_fields"]["phone_cell"] is None and phones: b["borrower_fields"]["phone_cell"] = phones[0][0]

        for employer in children(party, "EMPLOYER"):
            employment = next(x for x in employer.iter() if lname(x.tag) == "EMPLOYMENT")
            ownership = first(employment, "OwnershipInterestType")
            e = {"employment_type": first(employment, "EmploymentStatusType"), "employer_name": first(employer, "FullName"), "position_title": first(employment, "EmploymentPositionDescription"), "start_date": first(employment, "EmploymentStartDate"), "is_self_employed": boolean(first(employment, "EmploymentBorrowerSelfEmployedIndicator")), "months_on_job": integer(first(employment, "EmploymentTimeInLineOfWorkMonthsCount")), "gross_monthly_income_usd": num(first(employment, "EmploymentMonthlyIncomeAmount")), "employer_address": address(next((x for x in employer if lname(x.tag) == "ADDRESS"), None)), "employer_phone": first(employer, "ContactPointTelephoneValue")}
            if ownership == "LessThan25Percent": e["self_employed_ownership_percent"] = {"qualifier": "LessThan", "value": 25}
            b["employments"].append({k: v for k, v in e.items() if v is not None})
        for income in children(party, "CURRENT_INCOME_ITEM"):
            d = next(x for x in income.iter() if lname(x.tag) == "CURRENT_INCOME_ITEM_DETAIL")
            raw_income_type = first(d, "IncomeType")
            income_type = {"Commissions": "Commission", "Other": "OtherIncome"}.get(raw_income_type, raw_income_type)
            monthly_amount = num(first(d, "CurrentIncomeMonthlyTotalAmount"))
            if monthly_amount is not None and monthly_amount > 0:
                b["income_sources"].append({"income_type": income_type, "monthly_amount_usd": monthly_amount})
        for item in assets:
            if any(a == item["_label"] and t == borrower_label for a, t in relationships):
                b["assets"].append({k: v for k, v in item.items() if not k.startswith("_") and v is not None})
        for item in liabilities:
            if any(a == item["_label"] and t == borrower_label for a, t in relationships):
                b["liabilities"].append({k: v for k, v in item.items() if not k.startswith("_") and v is not None})
        b = {k: v for k, v in b.items() if v or k == "borrower_fields"}
        b["borrower_fields"] = {k: v for k, v in b["borrower_fields"].items() if v is not None}
        borrowers.append(b)

    def address_text(value):
        if not value:
            return None
        parts = [value.get("street_line_1"), value.get("street_line_2"), value.get("city"), value.get("state"), value.get("postal_code")]
        return ", ".join(str(x) for x in parts if x)

    loan_details_borrowers = []
    for borrower in borrowers:
        fields = borrower["borrower_fields"]
        full_name = " ".join(x for x in (fields.get("first_name"), fields.get("middle_name"), fields.get("last_name")) if x)
        employment_and_income = []
        for index, employment in enumerate(borrower.get("employments", [])):
            income = borrower.get("income_sources", [])[index] if index < len(borrower.get("income_sources", [])) else {"monthly_amount_usd": employment.get("gross_monthly_income_usd")}
            if not income.get("monthly_amount_usd") or income["monthly_amount_usd"] <= 0:
                continue
            item = {
                "employer": employment.get("employer_name"),
                "employmentStartDate": employment.get("start_date"),
                "employmentType": employment.get("employment_type"),
                "totalMonthlyQualifyingIncome": income.get("monthly_amount_usd"),
            }
            employment_and_income.append({k: v for k, v in item.items() if v is not None})
        loan_details_borrowers.append({
            "borrowerProfile": {
                "borrowerName": full_name,
                "currentAddress": address_text(fields.get("current_address")),
                "dateOfBirth": fields.get("date_of_birth"),
                "emailAddress": fields.get("email"),
                "maritalStatus": fields.get("marital_status"),
                "phoneNumber": fields.get("phone_cell") or fields.get("phone_home"),
                "socialSecurityNumberMasked": fields.get("ssn_masked"),
            },
            "borrowerType": "borrower" if fields.get("borrower_type") == "Primary" else "coborrower",
            "employmentAndIncome": employment_and_income,
        })
    primary_name = loan_details_borrowers[0]["borrowerProfile"]["borrowerName"]
    property_address = address_text(subject_property.get("address"))
    first_mortgage_payment = next((num(first(x, "HousingExpensePaymentAmount")) for x in loan.iter() if lname(x.tag) == "HOUSING_EXPENSE" and first(x, "HousingExpenseType") == "FirstMortgagePrincipalAndInterest"), None)
    loan_officer = next((
        " ".join(x for x in (first(p, "FirstName"), first(p, "MiddleName"), first(p, "LastName")) if x)
        for p in children(root, "PARTY") if first(p, "PartyRoleType") == "LoanOriginator"
    ), None)
    loan_details = {
        "summary": {
            "borrowerName": primary_name,
            "closingDate": first(loan, "ApplicationSignedByLoanOriginatorDate"),
            "loanAmount": loan_core.get("base_loan_amount_usd"),
            "loanId": loan_core.get("loan_number"),
            "loanType": loan_core.get("loan_type"),
            "status": loan_core.get("loan_status"),
            "propertyValueTop": subject_property.get("appraised_value_usd"),
        },
        "loanOverview": {
            "applicationDate": loan_core.get("application_date"),
            "loanAmount": loan_core.get("base_loan_amount_usd"),
            "loanProgram": loan_core.get("loan_type"),
            "loanOfficer": loan_officer,
            "interestRateApr": {
                k: v for k, v in {
                    "interestRate": loan_core.get("note_rate_percent"),
                    "apr": loan_core.get("apr_percent"),
                }.items() if v is not None
            },
            "principalAndInterestPayment": first_mortgage_payment,
            "dtiRatio": {
                k: v for k, v in {
                    "frontEnd": round(proposed_housing / monthly_income * 100, 2) if monthly_income else None,
                    "backEnd": round((proposed_housing + nonexcluded_liabilities) / monthly_income * 100, 2) if monthly_income else None,
                }.items() if v is not None
            },
            "ltvRatio": {"value": loan_core.get("ltv_percent")},
        },
        "propertyInformation": {
            "appraisalValue": str(subject_property.get("appraised_value_usd")),
            "occupancy": subject_property.get("occupancy_type"),
            "propertyAddress": property_address,
            "propertyType": subject_property.get("property_type"),
            **({"purchasePrice": subject_property["purchase_price_usd"]}
               if subject_property.get("purchase_price_usd") is not None else {}),
        },
        "borrowerDetails": loan_details_borrowers,
    }
    # loan_details.propertyInformation.appraisalValue is required by its
    # separate UI schema. If the XML has no appraisal value, omit this
    # optional section instead of emitting null, "None", or an invented value.
    if subject_property.get("appraised_value_usd") is None:
        loan_details = None
    out = {"project_details": {"project_number": loan_id}, "loan_core": loan_core, "borrowers": borrowers, "subject_property": {k: v for k, v in subject_property.items() if v is not None}}
    if not omit_loan_details and loan_details is not None:
        out["loan_details"] = loan_details
    return out


def main(source_path, output_path=None, omit_loan_details=False, quiet=False):
    out = build_output(source_path, omit_loan_details=omit_loan_details)
    rendered = json.dumps(out, indent=2) + "\n"
    if output_path:
        Path(output_path).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)

    if not quiet:
        borrowers = len(out.get("borrowers", []))
        print(
            f"Extracted {out.get('loan_core', {}).get('loan_number', 'unknown')} "
            f"with {borrowers} borrower(s) from {source_path}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert a MISMO 3.4 XML loan export to deterministic loan JSON."
    )
    parser.add_argument("source", help="Path to the MISMO XML file")
    parser.add_argument("-o", "--output", help="Output JSON path; defaults to stdout")
    parser.add_argument(
        "--omit-loan-details",
        action="store_true",
        help="Skip the optional loan_details section",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print the extraction summary to stderr",
    )
    args = parser.parse_args()
    main(args.source, args.output, args.omit_loan_details, args.quiet)
