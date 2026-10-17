"""Deterministic renewal calendar and owner-held policy comparison.

This module deliberately prepares and explains renewal work without estimating a premium,
selecting a product, or initiating a payment.  A sandbox invoice can be acted on only by the
existing tier-2 exact-payload confirmation path; a live mandate is an additional gate, not a
way around consent or confirmation.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Optional

from . import ingest, jurisdiction, motor, renewal

_OUTPUT_STATES = frozenset({
    "DOCUMENTED", "NOT_CONFIRMED", "INFORMATION_MISSING",
    "REQUIRES_AUTHORISED_REVIEW",
})


def _as_date(value: object) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _source(policy: Mapping[str, Any]) -> dict[str, Any]:
    ids = policy.get("source_document_ids")
    document_id = ids[0] if isinstance(ids, list) and len(ids) == 1 else None
    return {
        "source_document_id": document_id,
        "evidence_class": "PROVEN" if document_id else "UNESTABLISHED",
    }


def _policy_end(policy: Mapping[str, Any]) -> Optional[date]:
    period = policy.get("period")
    return _as_date(period.get("end")) if isinstance(period, Mapping) else None


def _timing_item(country: str, local_id: str, label: str) -> dict[str, Any]:
    """Expose only a sourced jurisdiction timing; a candidate produces no countdown."""
    try:
        entry = jurisdiction.pack(country).statement(local_id)
    except jurisdiction.PackError:
        entry = None
    if entry is None:
        return {
            "kind": local_id, "label": label, "state": "INFORMATION_MISSING",
            "days": None, "source": None,
            "required_document": "the policy wording or a sourced jurisdiction rule",
            "notice": "No countdown is shown until a cited policy or sourced rule establishes it.",
        }
    raw = entry.values.get("days")
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = None
    if days is None or days < 0:
        return {
            "kind": local_id, "label": label, "state": "INFORMATION_MISSING",
            "days": None, "source": entry.provenance(),
            "required_document": "the policy wording",
            "notice": "The cited rule does not establish a usable countdown value.",
        }
    return {
        "kind": local_id, "label": label, "state": "DOCUMENTED", "days": days,
        "source": entry.provenance(),
        "notice": "This is a cited timing reference, not a premium, coverage, or payment decision.",
    }


def calendar(portfolio: Mapping[str, Any], *, as_of: date) -> dict[str, Any]:
    """Return tenant-held renewal dates and only evidence-backed regulatory timing.

    Policy-period and renewal dates are stored policy facts.  Grace/free-look periods are
    deliberately omitted unless the selected jurisdiction pack has an owner-facing source.
    """
    if not isinstance(as_of, date):
        raise ValueError("as_of must be a date")
    items: list[dict[str, Any]] = []
    policies = portfolio.get("policies") if isinstance(portfolio, Mapping) else []
    for policy in policies if isinstance(policies, list) else []:
        if not isinstance(policy, Mapping):
            continue
        country = str(policy.get("country") or "GENERIC").upper()
        renewal_date = _as_date(policy.get("renewal_date")) or _policy_end(policy)
        source = _source(policy)
        if renewal_date is None:
            renewal_item = {
                "kind": "POLICY_RENEWAL", "policy_id": policy.get("id"),
                "policy": policy.get("product"), "country": country,
                "state": "INFORMATION_MISSING", "renewal_date": None,
                "days_to_renewal": None, "source": source,
                "required_document": "the policy schedule",
                "notice": "No renewal countdown is shown until the policy schedule establishes an end or renewal date.",
            }
        else:
            days = (renewal_date - as_of).days
            renewal_item = {
                "kind": "POLICY_RENEWAL", "policy_id": policy.get("id"),
                "policy": policy.get("product"), "country": country,
                "state": "DOCUMENTED", "renewal_date": renewal_date.isoformat(),
                "days_to_renewal": days, "source": source,
                "notice": "This calendar repeats the date held in the cited policy record; it does not quote or estimate a renewal premium.",
            }
        items.append(renewal_item)
        items.append({
            "policy_id": policy.get("id"), "policy": policy.get("product"),
            "country": country, **_timing_item(country, "TL-GRACE", "Grace-period timing"),
        })
        items.append({
            "policy_id": policy.get("id"), "policy": policy.get("product"),
            "country": country, **_timing_item(country, "TL-FREE-LOOK", "Free-look timing"),
        })
        if str(policy.get("line_of_business") or "").upper() == "MOTOR":
            alert = motor.third_party_alert(policy, as_of=as_of)
            items.append({
                "kind": "MOTOR_THIRD_PARTY", "policy_id": policy.get("id"),
                "policy": policy.get("product"), "country": country,
                "state": alert.get("state", "INFORMATION_MISSING"),
                "alert": alert.get("alert"), "days_to_expiry": alert.get("days_to_expiry"),
                "expires_on": alert.get("expires_on"), "source": alert.get("source"),
                "penalty": None, "notice": alert.get("penalty_notice") or alert.get("notice"),
            })
    return {
        "as_of": as_of.isoformat(), "items": items, "synthetic": True,
        "notice": (
            "Renewal dates come from the tenant-held policy record. Regulatory countdowns and compulsory alerts appear only when their cited source is owner-facing; no premium or coverage outcome is calculated."),
    }


def compare_documents(prior: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Diff two encrypted owner-held documents after the caller's tenant check.

    The API resolves both documents through the portfolio repository before it calls this
    function, so no document identifier is ever accepted as a filesystem path.
    """
    prior_blob, current_blob = prior.get("blob"), current.get("blob")
    if not isinstance(prior_blob, bytes) or not isinstance(current_blob, bytes):
        raise ValueError("Both selected policy documents must be readable owner-held documents.")
    prior_id = str(prior.get("id") or "")
    current_id = str(current.get("id") or "")
    prior_name = str(prior.get("filename") or "prior-policy.pdf")
    current_name = str(current.get("filename") or "current-policy.pdf")
    if not prior_id or not current_id:
        raise ValueError("Both selected policy documents need stable tenant document identifiers.")
    prior_pages = ingest.extract_pdf_pages_cached(prior_blob, prior_name)
    current_pages = ingest.extract_pdf_pages_cached(current_blob, current_name)
    result = renewal.compare(
        renewal.PolicyVersion.from_pages(prior_pages, prior_id, "Prior policy document"),
        renewal.PolicyVersion.from_pages(current_pages, current_id, "Current policy document"),
    )
    result["documents"] = {
        "prior": {"id": prior_id, "filename": prior_name},
        "current": {"id": current_id, "filename": current_name},
    }
    result["notice"] = (
        "This deterministic comparison quotes the two owner-held documents. It flags changes for review and does not decide cover, recommend a renewal, or state a premium.")
    return result


def payment_authorisation(store: Any, tenant_id: str, invoice: Mapping[str, Any], *,
                          notice_at: Optional[str]) -> dict[str, Any]:
    """Describe whether a scheduled sandbox invoice has a live mandate gate.

    This function has no executor and cannot transfer funds.  It exists so a scheduler can
    refuse tier-2 work before any external call; the existing agent confirmation remains the
    only execution path.
    """
    invoice_id = str(invoice.get("invoice_id") or "")
    payee = str(invoice.get("payee") or "")
    amount = str(invoice.get("amount") or "")
    owner = str(invoice.get("tenant_id") or "")
    if not invoice_id or not payee or not amount or owner != tenant_id:
        return {
            "state": "INFORMATION_MISSING", "eligible": False, "mandate": None,
            "notice": "The resolved sandbox invoice must belong to this tenant before a payment can be prepared.",
        }
    mandate = store.active_mandate(
        tenant_id, scope="invoice.pay", payee=payee, amount=amount, notice_at=notice_at)
    if mandate is None:
        return {
            "state": "REQUIRES_AUTHORISED_REVIEW", "eligible": False, "mandate": None,
            "invoice_id": invoice_id,
            "notice": "No live sandbox mandate matches this invoice's scope, payee, cap, window and recorded advance notice. No payment is prepared.",
        }
    return {
        "state": "DOCUMENTED", "eligible": True, "invoice_id": invoice_id,
        "mandate": {"id": mandate["id"], "scope": "invoice.pay",
                    "per_payment_cap": mandate["per_payment_cap"],
                    "payee_allowlist": mandate["payee_allowlist"],
                    "valid_until": mandate["valid_until"],
                    "advance_notice_required": mandate["advance_notice_required"]},
        "confirmation_required": True,
        "notice": "A matching mandate permits preparation only. Exact-payload confirmation and step-up authentication remain required before the local synthetic payment path can run.",
    }
