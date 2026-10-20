"""Static contracts for the owner-facing P0 judge-readiness surfaces."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "JUDGE-READINESS.md").read_text(encoding="utf-8")


def test_guided_demo_uses_the_labelled_path_and_existing_safe_reset():
    assert "function judgeDemoCard" in JS
    assert 'data-act="load-household-example"' in JS
    assert 'data-act="demo-reset"' in JS
    assert "RESET_SYNTHETIC_EXAMPLE" in JS
    assert "preserves uploads and ledger history" in DOC


def test_receipt_projects_existing_agent_fields_and_states_boundaries():
    receipt = JS[JS.index("function agentActionReceipt"):JS.index("async function smeClaims")]
    for field in ("agentStatus.consent", "plan?.steps", "payload_hash", "ledger?.verification"):
        assert field in receipt
    assert "boundaryBadges()" in receipt
    assert "NO REAL CLAIM SUBMISSION" in (ROOT / "web" / "i18n" / "en.json").read_text(encoding="utf-8")


def test_source_drawer_and_boundaries_remain_keyboard_accessible():
    assert 'ev.key === "Escape"' in JS
    assert "closeEvidenceDrawer" in JS
    assert ".boundary-badges" in CSS and ".agent-receipt__grid" in CSS


def test_submission_artifact_states_the_five_minute_runbook_and_limits():
    assert "Five-minute owner demo" in DOC
    assert "not a real insurer, bank, TPA, payment, claim-approval" in DOC
    assert "Security risk → control → evidence" in DOC
    assert "python -m pytest -q -p no:warnings" in DOC
