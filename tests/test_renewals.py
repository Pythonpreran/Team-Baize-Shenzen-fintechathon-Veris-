"""T22 renewal calendar, owner-document diff, and mandate gate tests."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pdfbuild_renewal as renewal_pdf

from proven import persistence, renewals
from proven.agent import AgentStore


def _motor_policy() -> dict:
    return {
        "id": "policy-motor", "source": "SYNTHETIC_EXAMPLE", "country": "IN",
        "line_of_business": "MOTOR", "product": "Private car plan (synthetic schedule)",
        "masked_number": "****-MOTOR7", "source_document_ids": ["motor-doc"],
        "period": {"start": "2025-10-19", "end": "2026-10-18"},
        "renewal_date": "2026-10-18",
    }


def test_renewal_calendar_keeps_countdowns_evidence_bound_and_withholds_unsourced_penalties():
    result = renewals.calendar({"policies": [_motor_policy()]}, as_of=date(2026, 10, 9))
    renewal = next(item for item in result["items"] if item["kind"] == "POLICY_RENEWAL")
    alert = next(item for item in result["items"] if item["kind"] == "MOTOR_THIRD_PARTY")
    grace = next(item for item in result["items"] if item["kind"] == "TL-GRACE")
    assert renewal["state"] == "DOCUMENTED"
    assert renewal["days_to_renewal"] == 9
    assert alert["state"] == "EXPIRING"
    assert alert["penalty"] is None
    assert grace["state"] == "INFORMATION_MISSING"
    assert grace["days"] is None


def test_owner_document_diff_reuses_deterministic_renewal_engine_without_a_coverage_verdict():
    result = renewals.compare_documents(
        {"id": "prior", "filename": "prior.pdf", "blob": renewal_pdf.prior_policy_pdf()},
        {"id": "current", "filename": "current.pdf", "blob": renewal_pdf.current_policy_pdf()},
    )
    assert result["change_count"] >= 1
    assert result["documents"]["prior"]["id"] == "prior"
    assert all(change["materiality"] in {"MATERIAL", "ADMINISTRATIVE", "UNKNOWN"}
               for change in result["changes"])
    assert "does not decide cover" in result["notice"].lower()


def test_scheduler_payment_gate_needs_matching_live_mandate_and_keeps_confirmation_required(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite", b"r" * 32)
    scope = persistence.TenantScope("tenant-a", "user-a", role="SME")
    invoice = {"invoice_id": "SBX-INV-1", "tenant_id": scope.tenant_id,
               "payee": "Lotus General (Sandbox)", "amount": "10.00"}
    blocked = renewals.payment_authorisation(store, scope.tenant_id, invoice, notice_at=None)
    assert blocked["eligible"] is False
    created = store.create_mandate(
        scope.tenant_id, scopes=["invoice.pay"], per_payment_cap="10.00",
        payee_allowlist=["Lotus General (Sandbox)"], valid_from=datetime.now(timezone.utc).date().isoformat(),
        valid_until=(datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat(), advance_notice_required=True)
    ready = renewals.payment_authorisation(
        store, scope.tenant_id, invoice,
        notice_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    assert ready["eligible"] is True
    assert ready["mandate"]["id"] == created["id"]
    assert ready["confirmation_required"] is True
