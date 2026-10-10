"""Static contracts for T04's dependency-free frontend localization layer."""
from pathlib import Path

from fastapi.testclient import TestClient

from proven.api import app

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


def test_frontend_loads_its_english_catalogue_and_has_a_pseudo_locale():
    assert '"/static/i18n/en.json"' in JS
    assert "function t(" in JS
    assert "PSEUDO_LOCALE" in JS
    assert '"set-locale"' in JS


def test_shell_navigation_uses_data_actions_not_assigned_click_handlers():
    shell = JS[JS.index("function shell("):JS.index("/* ═════════", JS.index("function shell("))]
    assert 'data-act="nav"' in shell
    assert ".onclick" not in shell
    assert "languageSelector()" in shell
    assert 'data-act="set-locale"' in JS


def test_translation_catalogue_is_served_as_a_static_asset():
    with TestClient(app) as client:
        response = client.get("/static/i18n/en.json")
    assert response.status_code == 200
    assert response.json()["shell"]["language"] == "Language"
