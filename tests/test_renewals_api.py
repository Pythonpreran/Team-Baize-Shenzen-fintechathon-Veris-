"""T22 tenant-scoped renewal calendar and owner-document comparison API tests."""
from __future__ import annotations

import base64
import os

import pdfbuild_renewal as renewal_pdf
from fastapi.testclient import TestClient

from proven import api, auth, persistence


OWNER = {"email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD}


def test_renewal_routes_use_only_the_signed_in_tenants_documents(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    api.SESSIONS.clear()
    with TestClient(api.app) as client:
        assert client.post("/api/auth/login", json=OWNER).status_code == 200
        session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
        assert session is not None
        with session.lock:
            scope = api._portfolio_scope(session)
        repository = api._portfolio_repository()
        prior = repository.add_document(
            scope, kind="SCHEDULE", blob=renewal_pdf.prior_policy_pdf(),
            language="en", country="IN", filename="prior-policy.pdf")
        current = repository.add_document(
            scope, kind="SCHEDULE", blob=renewal_pdf.current_policy_pdf(),
            language="en", country="IN", filename="current-policy.pdf")
        repository.create_policy(
            scope, country="IN", line_of_business="MOTOR", insurer="Lotus General (Sandbox)",
            product="Synthetic renewal policy", masked_number="****-RNEW1", status="ACTIVE",
            period_end="2026-10-18", renewal_date="2026-10-18",
            source_document_ids=[current["id"]])
        calendar = client.get("/api/renewals")
        assert calendar.status_code == 200, calendar.text
        assert any(item["kind"] == "POLICY_RENEWAL" for item in calendar.json()["items"])
        compared = client.post("/api/renewals/compare", json={
            "prior_document_id": prior["id"], "current_document_id": current["id"],
        })
        assert compared.status_code == 200, compared.text
        assert compared.json()["documents"]["current"]["id"] == current["id"]
    api.SESSIONS.clear()
