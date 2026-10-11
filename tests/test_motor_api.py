"""T16 tenant-scoped motor alert and depreciation API checks."""
from __future__ import annotations

import base64
import copy
import os

import pdfbuild_household
from fastapi.testclient import TestClient

from proven import api, auth, motor, persistence, sandbox


def test_motor_alert_and_depreciation_api_use_only_synthetic_schedule_evidence(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, key)
    api.SESSIONS.clear()
    with TestClient(api.app) as client:
        assert client.post("/api/auth/login", json={
            "email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD,
        }).status_code == 200
        session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
        assert session is not None
        with session.lock:
            scope = api._portfolio_scope(session)
        repo = api._portfolio_repository()
        document = repo.add_document(scope, kind="SCHEDULE", blob=pdfbuild_household.india_motor_schedule_pdf(),
                                     language="en", country="IN", filename="rao-india-motor-schedule.pdf")
        policy = repo.create_policy(scope, country="IN", line_of_business="MOTOR",
                                    insurer="Lotus General (Sandbox)", product="Private car plan (synthetic schedule)",
                                    masked_number="****-MOTOR7", status="ACTIVE", period_end="2026-10-18",
                                    source_document_ids=[document["id"]])
        original = motor.enrich_portfolio

        def stamp(raw):
            value = copy.deepcopy(raw)
            selected = next(item for item in value["policies"] if item["id"] == policy["id"])
            selected["source"] = "SYNTHETIC_EXAMPLE"
            return original(value)

        monkeypatch.setattr(motor, "enrich_portfolio", stamp)
        alert = client.get(f"/api/motor/{policy['id']}/alert")
        assert alert.status_code == 200, alert.text
        assert alert.json()["alert"]["days_to_expiry"] == 9
        assert alert.json()["alert"]["penalty"] is None
        assert client.post("/api/agent/consent", json={"scopes": ["agent:read"]}).status_code == 200
        what_if = client.post(f"/api/motor/{policy['id']}/depreciation-what-if", json={
            "part_material": "plastic", "repair_cost": "10000",
        })
        assert what_if.status_code == 200, what_if.text
        assert what_if.json()["lines"][2]["amount"] == "5000.00"
    api.SESSIONS.clear()
