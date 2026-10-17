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


def test_the_chinese_catalogue_is_served_and_loaded_only_for_owner_screens():
    """T28: zh-CN is a selectable locale whose catalogue is fetched same-origin, falls
    back to English key by key, and declares which views it covers."""
    assert '"/static/i18n/zh-CN.json"' in JS
    assert 'const ZH_LOCALE = "zh-CN"' in JS
    assert "ZH_TRANSLATED_VIEWS" in JS and 't("locale.english_only")' in JS
    with TestClient(app) as client:
        response = client.get("/static/i18n/zh-CN.json")
    assert response.status_code == 200
    body = response.json()
    assert body["shell"]["language"] == "语言"
    assert "reviewer" not in body, "the reviewer console stays English"
    assert set(body["nav"]) == {"sme"}, "adviser and insurer navigation stays English"
    reviewer_js = (ROOT / "web" / "reviewer.js").read_text(encoding="utf-8")
    assert "/static/i18n/zh-CN.json" in reviewer_js and "english_only" in reviewer_js


def test_a_value_control_is_dispatched_on_change_and_never_on_its_opening_click():
    """REGRESSION. The language menu closed within milliseconds of opening.

    The delegated CLICK listener fired on the very click that opened the native option
    list: `dispatchAction` resolved the <select> as the nearest `[data-act]`, called
    `preventDefault()` and then `setLocale()` with a value that had not changed yet, and
    `setLocale`'s `render()` rewrote `#app.innerHTML` — destroying the open select.

    The fix has to stay in the click path. A control whose action depends on its VALUE is
    clicked BEFORE the value changes, so it is dispatched on `change` only.
    """
    dispatch = JS[JS.index("function dispatchAction("):JS.index("document.addEventListener(\"click\"")]
    assert 'ev.type === "click"' in dispatch and "isValueControl(el)" in dispatch
    guard = JS[JS.index("function isValueControl("):JS.index("function dispatchAction(")]
    assert '"SELECT"' in guard and '"TEXTAREA"' in guard
    # Buttons, checkboxes and radios still act on the click, where the click IS the
    # decision; only free-value controls wait for `change`.
    assert "button|submit|reset|checkbox|radio|image" in guard


def test_choosing_the_locale_already_shown_does_not_repaint_the_shell():
    """A repaint replaces the picker the reader is holding, so it must not happen for a
    choice that changes nothing."""
    set_locale = JS[JS.index("function setLocale("):JS.index("function languageSelector(")]
    assert "const normalized" in set_locale
    assert "if (normalized === S.locale) return Promise.resolve();" in set_locale
    assert set_locale.index("return Promise.resolve();") < set_locale.index("render();")


def test_the_locale_action_puts_the_keyboard_back_on_the_picker_it_repainted():
    action = JS[JS.index('"set-locale":'):JS.index("logout: () => logout()")]
    assert "const id = el.id;" in action
    assert "document.getElementById(id)?.focus()" in action
