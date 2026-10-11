"""T13 local health-claim intake, packets, deadline and state-machine safeguards."""
from __future__ import annotations

import hashlib

import pytest

from proven import claims, persistence


@pytest.fixture()
def repository(tmp_path):
    return claims.ClaimsRepository(tmp_path / "claims.sqlite", b"c" * 32)


@pytest.fixture()
def scope():
    return persistence.TenantScope("tenant-a", "user-a", "Owner", "owner@example.test", "SME")


def _policy() -> dict:
    return {
        "id": "policy-health", "country": "IN", "line_of_business": "HEALTH",
        "source": "SYNTHETIC_EXAMPLE", "source_document_ids": ["doc-schedule"],
        "health_items": [
            {"mechanism_code": "DEADLINE.CLAIM_INTIMATION", "state": "DOCUMENTED",
             "value": "24", "unit": "hours", "citation": {
                 "document": "Synthetic policy wording", "page": 7,
                 "quote": "Notification of claim must be made within 24 hours.",
             }},
            {"mechanism_code": "COVERED.FAMILY_FLOATER", "citation": {
                 "document": "Synthetic schedule", "page": 1,
                 "quote": "Synthetic family-floater sum insured: INR 500,000.",
             }},
            {"mechanism_code": "SHARE.CO_PAYMENT", "state": "DOCUMENTED", "value": "5", "unit": "%",
             "citation": {"document": "Synthetic policy wording", "page": 9,
                          "quote": "The insured shall bear five per cent of each admissible claim."}},
            {"mechanism_code": "CONDITION.PROPORTIONATE_DEDUCTION", "state": "DOCUMENTED",
             "citation": {"document": "Synthetic policy wording", "page": 10,
                          "quote": "A proportionate deduction applies only to associated medical expenses."}},
        ],
    }


def _packet_documents() -> list[dict[str, str]]:
    return [
        {"id": "doc-schedule", "kind": "SCHEDULE", "sha256": hashlib.sha256(b"schedule").hexdigest()},
        {"id": "doc-discharge", "kind": "HOSPITAL_DISCHARGE_SUMMARY", "sha256": hashlib.sha256(b"discharge").hexdigest()},
        {"id": "doc-bill", "kind": "HOSPITAL_BILL", "sha256": hashlib.sha256(b"bill").hexdigest()},
    ]


def test_health_intake_builds_a_cited_checklist_and_policy_timezone_deadline(repository, scope):
    claim = repository.create(scope, policy=_policy(), incident_at="2026-10-10T09:00:00+00:00",
                              mode="REIMBURSEMENT")
    assert claim["state"] == "DRAFTING"
    assert claim["incident_at"] == "2026-10-10T14:30:00+05:30"
    assert claim["deadline"]["state"] == "DOCUMENTED"
    assert claim["deadline"]["due_at"] == "2026-10-11T14:30:00+05:30"
    assert all(item["reason"] and item["source"] for item in claim["checklist"])
    assert {item["document_kind"] for item in claim["checklist"] if item["packet_required"]} == {
        "SCHEDULE", "HOSPITAL_DISCHARGE_SUMMARY", "HOSPITAL_BILL"}


def test_claim_intake_refuses_timezone_ambiguous_incident_time(repository, scope):
    with pytest.raises(claims.ClaimValidationError, match="explicit UTC offset"):
        repository.create(scope, policy=_policy(), incident_at="2026-10-10T09:00:00", mode="REIMBURSEMENT")


def test_missing_deadline_operand_yields_no_calculated_date(repository, scope):
    policy = _policy()
    policy["health_items"][0]["state"] = "INFORMATION_MISSING"
    policy["health_items"][0]["value"] = None
    claim = repository.create(scope, policy=policy, incident_at="2026-10-10T09:00:00+00:00",
                              mode="CASHLESS")
    assert claim["deadline"]["due_at"] is None
    assert claim["deadline"]["required_document"] == "the policy wording"


def test_packet_requires_every_required_checklist_role(repository, scope):
    claim = repository.create(scope, policy=_policy(), incident_at="2026-10-10T09:00:00+00:00",
                              mode="REIMBURSEMENT")
    with pytest.raises(claims.ClaimValidationError, match="Hospital discharge summary"):
        repository.packet(scope, claim["id"], _packet_documents()[:1])


def test_packet_manifest_detects_a_swapped_document(repository, scope):
    claim = repository.create(scope, policy=_policy(), incident_at="2026-10-10T09:00:00+00:00",
                              mode="REIMBURSEMENT")
    packet = repository.packet(scope, claim["id"], _packet_documents())
    assert packet["checklist_complete"] is True
    assert repository.verify_packet(packet) is True
    swapped = {**packet, "manifest": {**packet["manifest"], "documents": [
        {"document_id": "doc-schedule", "sha256": hashlib.sha256(b"bill").hexdigest(), "kind": "SCHEDULE"},
        {"document_id": "doc-discharge", "sha256": hashlib.sha256(b"discharge").hexdigest(), "kind": "HOSPITAL_DISCHARGE_SUMMARY"},
        {"document_id": "doc-bill", "sha256": hashlib.sha256(b"schedule").hexdigest(), "kind": "HOSPITAL_BILL"},
    ]}}
    assert repository.verify_packet(swapped) is False


def test_transition_actor_rules_are_enforced(repository, scope):
    claim = repository.create(scope, policy=_policy(), incident_at="2026-10-10T09:00:00+00:00",
                              mode="REIMBURSEMENT")
    ready = repository.transition(scope, claim["id"], to_state="READY", actor="AGENT")
    assert ready["state"] == "READY"
    with pytest.raises(claims.ClaimTransitionError):
        repository.transition(scope, claim["id"], to_state="SUBMITTED", actor="OWNER")
    submitted = repository.transition(scope, claim["id"], to_state="SUBMITTED", actor="AGENT")
    acknowledged = repository.transition(scope, claim["id"], to_state="ACKNOWLEDGED", actor="INSURER")
    assert submitted["state"] == "SUBMITTED" and acknowledged["state"] == "ACKNOWLEDGED"
    loaded = repository.get(scope, claim["id"])
    assert [row["actor"] for row in loaded["transitions"]] == ["OWNER", "AGENT", "AGENT", "INSURER"]


def test_claim_tenant_isolation_and_evidence_bound_deduction_classification(repository, scope):
    claim = repository.create(scope, policy=_policy(), incident_at="2026-10-10T09:00:00+00:00",
                              mode="REIMBURSEMENT")
    other = persistence.TenantScope("tenant-b", "user-b", "Other", "other@example.test", "SME")
    with pytest.raises(claims.ClaimNotFound):
        repository.get(other, claim["id"])
    classified = claims.classify_deductions(_policy(), {"deductions": [
        {"id": "supported", "amount": "1.00", "status": "SUPPORTED_BY_SCENARIO",
         "evidence_mechanism": "SHARE.CO_PAYMENT"},
        {"id": "unfound", "amount": "2.00", "status": "NOT_FOUND_IN_SCENARIO_WORDING",
         "evidence_mechanism": "CONDITION.PROPORTIONATE_DEDUCTION",
         "evidence_mode": "ABSENCE_FROM_DEFINED_SCOPE"},
        {"id": "unbound", "amount": "3.00", "status": "NOT_FOUND_IN_SCENARIO_WORDING"},
    ]})
    assert [row["classification"] for row in classified] == ["SUPPORTED", "NOT_FOUND", "NEEDS_REVIEW"]
    assert classified[1]["citation"]["quote"] == (
        "A proportionate deduction applies only to associated medical expenses.")


def test_escalation_fails_closed_when_the_first_complaint_step_is_not_sourced(repository, scope):
    claim = repository.create(scope, policy=_policy(), incident_at="2026-10-10T09:00:00+00:00",
                              mode="REIMBURSEMENT")
    gate = claims.escalation_gate(
        _policy(), claim, now_at="2026-10-12T14:31:00+05:30")
    assert gate["output_state"] == "INFORMATION_MISSING"
    assert gate["eligible"] is False
    assert gate["entry"] is None
    assert "first complaint-ladder step" in gate["notice"]
