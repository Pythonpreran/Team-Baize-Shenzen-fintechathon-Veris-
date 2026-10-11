"""T17 portfolio-map API and self-authored life-event boundary."""
from __future__ import annotations

import base64
import os

import pdfbuild_household
from fastapi.testclient import TestClient

from proven import api, auth, household_loader, persistence, sandbox


def test_protection_map_shows_documented_overlap_compulsory_gap_and_neutral_unassessed_area(tmp_path, monkeypatch):
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
        sources = {name: build() for name, build in pdfbuild_household.BUILDERS.items()}
        household_loader.load_example(api._portfolio_repository(), scope, sources)
        mapped = client.get("/api/protection-map")
        assert mapped.status_code == 200, mapped.text
        body = mapped.json()
        cells = {(row["entity"], row["risk"]): row for row in body["cells"]}
        assert cells[("Arjun Rao", "hospitalisation")]["state"] == "DOCUMENTED_TERMS"
        assert cells[("Anya Rao", "accident")]["state"] == "NOT_ASSESSED"
        motor = cells[("Bengaluru family car", "motor_third_party")]
        assert motor["state"] == "COMPULSORY_GAP"
        assert motor["source"]["status"] == "SOURCE_QUOTED"
        assert body["overlaps"] and len(body["overlaps"][0]["policy_ids"]) == 2
        assert all(row["separate_policy_limit_illustration"] is None
                   for row in body["overlaps"][0]["illustrations"])
        scenario = client.get("/api/protection-map", params={"scenario_loss": "100000"})
        assert scenario.status_code == 200, scenario.text
        assert {row["separate_policy_limit_illustration"]
                for row in scenario.json()["overlaps"][0]["illustrations"]} == {"100000"}
        assert body["next_document"]["unbounded"] is True

        event = client.post("/api/protection-map/events", json={"code": "NEW_BABY"})
        assert event.status_code == 200, event.text
        assert event.json()["evidence_class"] == "DECLARED"
        after = client.get("/api/protection-map").json()
        assert after["cells"] == body["cells"]
    api.SESSIONS.clear()
