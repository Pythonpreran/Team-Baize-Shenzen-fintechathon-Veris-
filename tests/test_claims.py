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


# ── checklist citations (regression: every item cited "the policy schedule") ──

def _health_policy_with_reimbursement_wording() -> dict:
    """The demo health policy plus the claim-procedure clause HEALTH.yaml actually maps."""
    policy = _policy()
    policy["health_items"].append({
        "mechanism_code": "CONDITION.REIMBURSEMENT", "state": "DOCUMENTED",
        "citation": {"document": "Synthetic policy wording", "page": 12,
                     "quote": "Procedure for reimbursement of claims: submit the necessary documents."},
    })
    return policy


def test_each_checklist_item_cites_its_own_source_not_one_shared_label():
    """MEASURED DEFECT. All four items showed the source "the policy schedule".

    Two causes, both in claims.py: the health branch looked up `CLAIM.REIMBURSEMENT`
    while HEALTH.yaml emits `CONDITION.REIMBURSEMENT`, so nothing matched; and the
    unmatched branch then labelled the item with the schedule — a document that
    establishes none of those requirements. A fabricated citation is the one output this
    product may not produce (PLAN rule 15).
    """
    items = claims.checklist_for(_health_policy_with_reimbursement_wording(), "REIMBURSEMENT")
    by_item = {item["item"]: item["source"] for item in items}
    procedure = by_item["Itemised hospital bills"]
    assert procedure["page"] == 12, "the claim-procedure clause was not matched"
    assert procedure["quote"].startswith("Procedure for reimbursement")
    assert by_item["Hospital discharge summary"]["page"] == 12
    # The schedule item is the one item the schedule genuinely establishes.
    assert by_item["Policy schedule"]["document"] == "Synthetic schedule"
    assert by_item["Claim notice details"]["page"] == 7
    # And no single label is doing the work of the whole list any more.
    assert len({(s["document"], s["page"]) for s in by_item.values()}) > 1
    assert not any(s["document"] == "the policy schedule" for s in by_item.values())


def test_an_unestablished_checklist_item_says_so_and_names_what_would_establish_it():
    """A policy whose wording carries no claim procedure. The items that rest on one are
    reported as open questions, never relabelled onto a document that is silent."""
    items = claims.checklist_for(_policy(), "REIMBURSEMENT")
    by_item = {item["item"]: item["source"] for item in items}
    for name in ("Hospital discharge summary", "Itemised hospital bills"):
        source = by_item[name]
        assert source, "the record must stay truthy so callers can keep one check"
        assert source["state"] == "INFORMATION_MISSING", name
        assert source["document"] is None and source["quote"] is None, name
        assert source["required_document"] == "the policy wording's claim procedure", name
    assert not any(s["document"] == "the policy schedule" for s in by_item.values())


def test_a_motor_police_report_is_not_cited_to_the_third_party_cover_entry():
    """A police report is established by an incident record or a wording clause. Citing
    the third-party cover entry said a document requires a report when nothing read did."""
    policy = {"id": "policy-motor", "country": "IN", "line_of_business": "MOTOR",
              "source": "SYNTHETIC_EXAMPLE", "source_document_ids": ["doc-motor"],
              "motor_items": [{"mechanism_code": "COVERED.MOTOR_THIRD_PARTY",
                               "state": "DOCUMENTED", "source_document_id": "doc-motor"}]}
    items = {item["item"]: item["source"] for item in claims.checklist_for(policy, "REIMBURSEMENT")}
    report = items["Police report, if one exists"]
    assert report["state"] == "INFORMATION_MISSING"
    assert report["document"] is None
    assert report["required_document"] == "the policy wording's claim procedure"
    # The schedule item still carries the entry that does establish it.
    assert items["Policy schedule"]["source_document_id"] == "doc-motor"


def test_a_motor_vehicle_registration_is_not_cited_to_the_third_party_cover_entry():
    """The same mislabel, one item along, and the one the first pass left behind.

    "Vehicle registration" shared the schedule item's citation object, so a third-party
    COVER entry — quote and page included — was presented as the source for a vehicle
    identifier requirement. Nothing mapped establishes that requirement, so the item
    reports an open question and names the document that would close it.
    """
    policy = {"id": "policy-motor", "country": "IN", "line_of_business": "MOTOR",
              "source": "SYNTHETIC_EXAMPLE", "source_document_ids": ["doc-motor"],
              "motor_items": [{"mechanism_code": "COVERED.MOTOR_THIRD_PARTY",
                               "state": "DOCUMENTED", "source_document_id": "doc-motor",
                               "citation": {"document": "Synthetic motor schedule", "page": 2,
                                            "quote": "Third-party liability is covered."}}]}
    items = {item["item"]: item["source"] for item in claims.checklist_for(policy, "REIMBURSEMENT")}
    registration = items["Vehicle registration"]
    assert registration["state"] == "INFORMATION_MISSING"
    assert registration["document"] is None and registration["quote"] is None
    assert registration["page"] is None
    assert registration["required_document"] == "the vehicle registration certificate"
    # The cover entry stays where it belongs: on the item it does establish.
    assert items["Policy schedule"]["quote"] == "Third-party liability is covered."
    assert registration["quote"] != items["Policy schedule"]["quote"]
    assert not any(source.get("quote") == "Third-party liability is covered."
                   for name, source in items.items() if name != "Policy schedule")


def test_the_schedule_label_is_never_attached_to_a_wording_clause():
    """A mapped term that carries a document id but no citation used to be announced as
    "your uploaded policy schedule" from the generic path, so a claim-procedure or
    cashless-network clause mapped from the WORDING could be labelled the schedule. That
    reading belongs to the schedule item alone."""
    policy = {"id": "policy-health", "country": "SG", "line_of_business": "HEALTH",
              "source": "SYNTHETIC_EXAMPLE", "source_document_ids": ["doc-schedule"],
              "health_items": [
                  {"mechanism_code": "CONDITION.REIMBURSEMENT", "state": "DOCUMENTED",
                   "source_document_id": "doc-wording"},
                  {"mechanism_code": "CONDITION.CASHLESS_NETWORK", "state": "DOCUMENTED",
                   "source_document_id": "doc-wording"}]}
    items = {item["item"]: item["source"] for item in claims.checklist_for(policy, "REIMBURSEMENT")}
    for name in ("Hospital discharge summary", "Itemised hospital bills"):
        source = items[name]
        assert source["state"] == "INFORMATION_MISSING", name
        assert source["document"] is None, name
        # The id the mapped term named is kept; only the assumed label is gone.
        assert source["source_document_id"] == "doc-wording", name
    schedule = items["Policy schedule"]
    assert schedule["state"] == "DOCUMENTED"
    assert schedule["document"] == "the policy schedule"
    assert schedule["source_document_id"] == "doc-schedule"
    assert sum(1 for s in items.values() if s["document"] == "the policy schedule") == 1
