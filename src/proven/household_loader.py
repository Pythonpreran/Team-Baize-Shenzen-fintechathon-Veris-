"""Persist the verified synthetic household example through the portfolio repository."""
from __future__ import annotations

from collections.abc import Mapping

from . import household, persistence


def _completed_policy_numbers() -> set[str]:
    return {policy.masked_number for policy in household.POLICIES}


def load_example(
    repository: persistence.PortfolioRepository,
    scope: persistence.TenantScope,
    source_documents: Mapping[str, bytes],
) -> dict:
    """Create the synthetic household once after API-side upload verification.

    The input is deliberately raw bytes rather than caller-selected policy fields.  The
    API verifies the byte hashes against the generated PDFs before reaching this function;
    all members, policies, coverage values and evidence classes are then derived by the
    repository from this reviewed synthetic fixture.
    """
    expected = set(household.document_names())
    supplied = set(source_documents)
    if supplied != expected:
        raise persistence.PortfolioValidationError(
            "The synthetic household documents are incomplete or do not match the example.")
    if any(not isinstance(blob, bytes) or not blob for blob in source_documents.values()):
        raise persistence.PortfolioValidationError(
            "Every synthetic household document must contain the verified PDF bytes.")

    existing = repository.list_policies(scope)
    numbers = _completed_policy_numbers()
    existing_numbers = {str(policy.get("masked_number") or "") for policy in existing}
    if numbers <= existing_numbers:
        return repository.portfolio(scope)
    if numbers & existing_numbers:
        raise persistence.PortfolioValidationError(
            "The synthetic household example was only partly saved; clear that local "
            "portfolio before loading it again so no duplicate records are created.")

    members = {
        spec.key: repository.add_member(
            scope,
            name=spec.name,
            relationship=spec.relationship,
            date_of_birth=spec.date_of_birth,
            health_facts=spec.health_facts,
        )
        for spec in household.MEMBERS
    }
    household_id = next(iter(members.values()))["household_id"]
    repository.mark_synthetic_example(
        scope,
        household_id=household_id,
        name=household.HOUSEHOLD_NAME,
        seed_id=household.SEED_ID,
    )
    assets = {
        spec.key: repository.add_asset(
            scope,
            kind=spec.kind,
            name=spec.name,
            household_id=household_id,
        )
        for spec in household.ASSETS
    }
    documents = {
        spec.filename: repository.add_document(
            scope,
            kind=spec.kind,
            blob=source_documents[spec.filename],
            language="en",
            country=spec.country,
            filename=spec.filename,
        )
        for spec in household.DOCUMENTS
    }

    for policy_index, policy in enumerate(household.POLICIES):
        source_document = documents[policy.document_filename]
        source_id = source_document["id"]
        repository.create_policy(
            scope,
            country=policy.country,
            line_of_business=policy.line_of_business,
            insurer=policy.insurer,
            product=policy.product,
            masked_number=policy.masked_number,
            period_start=policy.period_start,
            period_end=policy.period_end,
            status=policy.status,
            renewal_date=policy.renewal_date,
            household_id=household_id,
            insured_member_ids=[members[key]["id"] for key in policy.member_keys],
            insured_asset_ids=[assets[key]["id"] for key in policy.asset_keys],
            source_document_ids=[source_id],
            coverage_items=[
                {
                    "mechanism_code": item.mechanism_code,
                    "value": item.value,
                    "unit": item.unit,
                    "evidence_span_ids": [
                        f"synthetic:{policy.document_filename}:{item.mechanism_code}"
                    ],
                    "source_document_id": source_id,
                }
                for item in policy.coverage
            ],
            seed_binding={
                "seed_id": household.SEED_ID,
                "policy_index": policy_index,
                "document_id": source_id,
                "document_sha256": source_document["sha256"],
                "document_filename": policy.document_filename,
            },
        )

    return repository.portfolio(scope)
