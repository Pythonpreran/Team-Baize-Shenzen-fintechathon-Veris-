"""Persist the verified synthetic household example through the portfolio repository."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping

from . import household, persistence


def _completed_policy_numbers() -> set[str]:
    return {policy.masked_number for policy in household.POLICIES}


def _existing_example_bindings(
    repository: persistence.PortfolioRepository,
    scope: persistence.TenantScope,
    existing: list[dict],
    source_documents: Mapping[str, bytes],
) -> tuple[str, dict[str, dict[str, object]]] | None:
    """Return only a complete legacy household whose stored sources still match this seed.

    A full set of masked numbers is insufficient proof: another portfolio could happen to
    use those strings. The loader matches the complete policy layout and coverage values,
    then compares each encrypted document's retained filename and hash with the PDFs the
    API has just verified. Anything short of that requires an explicit clear and reseed.
    """
    expected_hashes = {
        filename: hashlib.sha256(blob).hexdigest()
        for filename, blob in source_documents.items()
    }
    household_id: str | None = None
    bindings: dict[str, dict[str, object]] = {}

    for policy_index, spec in enumerate(household.POLICIES):
        candidates = [
            policy for policy in existing
            if (policy.get("country") == spec.country
                and policy.get("line_of_business") == spec.line_of_business
                and policy.get("insurer") == spec.insurer
                and policy.get("product") == spec.product
                and policy.get("masked_number") == spec.masked_number
                and policy.get("status") == spec.status
                and policy.get("renewal_date") == spec.renewal_date
                and isinstance(policy.get("period"), Mapping)
                and policy["period"].get("start") == spec.period_start
                and policy["period"].get("end") == spec.period_end)
        ]
        if len(candidates) != 1:
            return None
        policy = candidates[0]
        policy_id = policy.get("id")
        current_household_id = policy.get("household_id")
        if not isinstance(policy_id, str) or not isinstance(current_household_id, str):
            return None
        if household_id is None:
            household_id = current_household_id
        elif household_id != current_household_id:
            return None

        document_ids = policy.get("source_document_ids")
        if not isinstance(document_ids, list) or len(document_ids) != 1:
            return None
        document_id = document_ids[0]
        if not isinstance(document_id, str):
            return None
        document = repository.get_document(scope, document_id)
        if (document.get("filename") != spec.document_filename
                or document.get("sha256") != expected_hashes.get(spec.document_filename)):
            return None

        coverage_items = policy.get("coverage_items")
        if not isinstance(coverage_items, list) or len(coverage_items) != len(spec.coverage):
            return None
        expected_coverage = {
            (item.mechanism_code, item.value, item.unit) for item in spec.coverage
        }
        actual_coverage = {
            (item.get("mechanism_code"), item.get("value"), item.get("unit"))
            for item in coverage_items if isinstance(item, Mapping)
        }
        if (len(actual_coverage) != len(spec.coverage)
                or actual_coverage != expected_coverage
                or any(item.get("source_document_id") != document_id
                       for item in coverage_items if isinstance(item, Mapping))):
            return None

        bindings[policy_id] = {
            "seed_id": household.SEED_ID,
            "policy_index": policy_index,
            "document_id": document_id,
            "document_sha256": document["sha256"],
            "document_filename": spec.document_filename,
        }

    return (household_id, bindings) if household_id is not None else None


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
        legacy = _existing_example_bindings(repository, scope, existing, source_documents)
        if legacy is None:
            raise persistence.PortfolioValidationError(
                "The existing household records cannot be verified for a safe upgrade; "
                "clear that local portfolio and upload the example again.")
        household_id, bindings = legacy
        repository.backfill_synthetic_example(
            scope,
            household_id=household_id,
            name=household.HOUSEHOLD_NAME,
            seed_id=household.SEED_ID,
            policy_bindings=bindings,
        )
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
