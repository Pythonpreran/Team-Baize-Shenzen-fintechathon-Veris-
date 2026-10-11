"""T17 whole-portfolio map states, overlaps, life events and document ranking."""
from __future__ import annotations

from pathlib import Path

from proven import protection_map


def _term(code: str, document: str, value: str | None = None) -> dict:
    return {"mechanism_code": code, "source_document_id": document,
            "evidence_class": "PROVEN", "value": value}


def _portfolio() -> dict:
    return {
        "members": [
            {"id": "arjun", "name": "Arjun Rao"}, {"id": "anya", "name": "Anya Rao"},
        ],
        "policies": [
            {"id": "health-a", "line_of_business": "HEALTH", "country": "SG",
             "product": "Plan A", "insured_member_ids": ["arjun"],
             "coverage_items": [
                 _term("COVERED.HOSPITALISATION", "doc-a"),
                 _term("LIMIT.SUM_INSURED", "doc-a", "1500000"),
             ]},
            {"id": "health-b", "line_of_business": "HEALTH", "country": "SG",
             "product": "Plan B", "insured_member_ids": ["arjun"],
             "coverage_items": [
                 _term("COVERED.HOSPITALISATION", "doc-b"),
                 _term("LIMIT.SUM_INSURED", "doc-b", "1000000"),
             ]},
            {"id": "motor-in", "line_of_business": "MOTOR", "country": "IN",
             "source": "SYNTHETIC_EXAMPLE", "product": "Private car plan (synthetic schedule)",
             "masked_number": "****-MOTOR7", "source_document_ids": ["motor-doc"],
             "period": {"end": "2026-10-18"}, "coverage_items": [
                 _term("COVERED.MOTOR_THIRD_PARTY", "motor-doc"),
             ]},
            {"id": "home", "line_of_business": "HOME", "country": "SG", "coverage_items": [
                _term("COVERED.HOME_CONTENTS", "home-doc"),
            ]},
        ],
    }


def test_map_derives_states_from_proven_policy_terms_and_a_sourced_compulsory_rule_only():
    mapped = protection_map.build(_portfolio())
    by_key = {(cell["entity"], cell["risk"]): cell for cell in mapped["cells"]}
    assert by_key[("Arjun Rao", "hospitalisation")]["state"] == protection_map.DOCUMENTED
    assert by_key[("Arjun Rao", "hospitalisation")]["evidence_class"] == "PROVEN"
    assert by_key[("Anya Rao", "accident")]["state"] == protection_map.NOT_ASSESSED
    motor = by_key[("Bengaluru family car", "motor_third_party")]
    assert motor["state"] == protection_map.COMPULSORY_GAP
    assert motor["source"]["status"] == "SOURCE_QUOTED"


def test_unestablished_or_unbound_terms_are_not_promoted_to_documented_cells():
    portfolio = _portfolio()
    portfolio["policies"][0]["coverage_items"][0]["evidence_class"] = "DECLARED"
    portfolio["policies"][1]["coverage_items"][0]["evidence_class"] = "UNESTABLISHED"
    portfolio["policies"][2]["source"] = "PORTFOLIO"
    mapped = protection_map.build(portfolio)
    by_key = {(cell["entity"], cell["risk"]): cell for cell in mapped["cells"]}
    assert by_key[("Arjun Rao", "hospitalisation")]["state"] == protection_map.NOT_ASSESSED
    assert ("Bengaluru family car", "motor_third_party") not in by_key


def test_overlaps_require_an_explicit_scenario_before_showing_numbers():
    without_scenario = protection_map.build(_portfolio())
    assert all(row["separate_policy_limit_illustration"] is None
               for row in without_scenario["overlaps"][0]["illustrations"])
    mapped = protection_map.build(_portfolio(), scenario_loss="100000")
    overlap = mapped["overlaps"][0]
    assert len(overlap["policy_ids"]) == 2
    assert {row["separate_policy_limit_illustration"] for row in overlap["illustrations"]} == {"100000"}
    assert overlap["other_insurance_terms"] == "INFORMATION_MISSING"
    assert "which policy" in overlap["notice"].lower()


def test_life_events_are_self_authored_and_do_not_change_map_state_or_evidence_class():
    before = protection_map.build(_portfolio())
    event = protection_map.life_event("new_baby")
    after = protection_map.build(_portfolio())
    assert event["evidence_class"] == "DECLARED"
    assert before["cells"] == after["cells"]


def test_next_document_ranks_the_unbounded_unassessed_area_first_and_has_no_percentage():
    mapped = protection_map.build(_portfolio())
    assert mapped["next_document"]["unbounded"] is True
    assert mapped["next_document"]["rank"] == 1
    assert mapped["next_document"]["percentage"] is None


def test_map_source_and_states_never_render_a_coverage_denial_phrase():
    source = (Path(__file__).resolve().parents[1] / "src" / "proven" / "protection_map.py").read_text(encoding="utf-8").lower()
    assert "not covered" not in source
    assert "will not pay" not in source
