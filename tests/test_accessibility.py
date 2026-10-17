"""T25 accessibility pass: contrast, focus, keyboard parity, live regions, motion.

Static checks over the shipped files, plus a contrast computation from the stylesheet's
own effective tokens. These are necessary conditions, not a certification: full WCAG
conformance needs manual testing with assistive technologies and an expert review.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
CSS = (WEB / "styles.css").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")


def _effective_tokens() -> dict[str, str]:
    """The LAST declaration of each custom property wins, as it does in the cascade."""
    tokens: dict[str, str] = {}
    for block in re.findall(r":root\s*\{(.*?)\}", CSS, flags=re.S):
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;", block):
            tokens[name] = value.lower()
    return tokens


def _luminance(hex_colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT_TOKENS = ("--text", "--text-2", "--text-3", "--text-4")
GROUNDS = ("--bg", "--surface", "--surface-alt", "--paper", "--jade-light", "--amber-light",
           "--clay-light", "--field-light")


@pytest.mark.parametrize("ink", TEXT_TOKENS)
@pytest.mark.parametrize("ground", GROUNDS)
def test_text_tokens_meet_aa_contrast_on_every_light_ground(ink, ground):
    tokens = _effective_tokens()
    ratio = _contrast(tokens[ink], tokens[ground])
    assert ratio >= 4.5, f"{ink} {tokens[ink]} on {ground} {tokens[ground]} is {ratio:.2f}:1"


def test_every_interactive_element_has_a_visible_focus_ring():
    assert ':where(a, button, input, select, textarea, summary, [tabindex], [role="button"]):focus-visible' in CSS
    rule = CSS[CSS.index(':where(a, button'):]
    assert "outline:3px solid" in rule[:200]


def test_a_skip_link_reaches_the_main_region_and_navigation_moves_focus():
    assert 'data-act="skip-main"' in JS and '"skip-main":' in JS
    assert 'id="main" tabindex="-1"' in JS
    assert ".skip-link:focus" in CSS
    nav = JS[JS.index("function nav8"):JS.index("window.nav8 = nav8")]
    assert "S.focusMain = true" in nav
    mount = JS[JS.index("async function mount"):JS.index("/* ── Login")]
    assert "focusMainHeading(false)" in mount and "enhanceKeyboard(el)" in mount


def test_action_cards_and_rows_are_reachable_and_operable_by_keyboard():
    enhance = JS[JS.index("function enhanceKeyboard"):JS.index("(function keepActionsKeyboardReachable")]
    assert 'setAttribute("tabindex", "0")' in enhance and 'setAttribute("role", "button")' in enhance
    assert 'event.key !== "Enter" && event.key !== " "' in enhance
    assert "dispatchAction(event)" in enhance
    assert "MutationObserver" in JS[JS.index("(function keepActionsKeyboardReachable"):]


def test_the_agent_trace_is_a_live_log_and_changes_are_announced():
    assert 'id="agent-trace" role="log" aria-live="polite"' in JS
    announce = JS[JS.index("function announce("):]
    assert 'setAttribute("aria-live", "polite")' in announce[:900]
    assert 'setAttribute("role", "status")' in announce[:900]
    assert "announceTrace(plan);" in JS


def test_the_protection_map_table_is_labelled_and_scrolls_by_keyboard():
    map_view = JS[JS.index("async function smeProtectionMap"):JS.index("function setProtectionScenario")]
    assert 'class="table-scroll" role="region" tabindex="0"' in map_view
    assert '<caption class="visually-hidden">' in map_view
    assert map_view.count('scope="col"') == 4


def _declared_colour(selector: str) -> str:
    """The LAST `color:` declared for a selector, resolved through the token table.

    The cascade reads the last declaration, so the test reads the last one too: an ink
    restated for a light ground further down the sheet is the ink on screen.
    """
    tokens = _effective_tokens()
    found: list[str] = []
    for block in re.findall(re.escape(selector) + r"\s*(?:,[^{]*)?\{(.*?)\}", CSS, flags=re.S):
        for declaration in block.split(";"):
            name, _, raw = declaration.partition(":")
            # `color`, not `border-top-color`: the property name must be the whole name.
            if name.strip() == "color" and raw.strip():
                found.append(raw.strip())
    assert found, f"no colour is declared for {selector}"
    value = found[-1]
    if value.startswith("var("):
        name = value[4:-1].strip()
        assert name in tokens, f"{selector} resolves to an unknown token {name}"
        return tokens[name]
    return value.lower()


@pytest.mark.parametrize("selector", (".verbatim__q", ".verbatim__src", ".verbatim__tag"))
@pytest.mark.parametrize("ground", ("--paper", "--surface-alt"))
def test_the_provenance_card_inks_are_readable_on_their_cream_ground(selector, ground):
    """MEASURED DEFECT, reported as "cannot view the text".

    `.verbatim` was built as a DARK navy card and later rethemed to cream without
    resetting its inks: the quotation stayed #e2e8f0 (1.13:1), the status banner #fca5a5
    (1.8:1). Text present in the DOM and invisible on screen. These ratios were measured
    in a browser once; this is what keeps them from drifting back.
    """
    ratio = _contrast(_declared_colour(selector), _effective_tokens()[ground])
    assert ratio >= 4.5, f"{selector} on {ground} is {ratio:.2f}:1"


def test_small_screens_keep_navigation_and_reduced_motion_is_honoured():
    assert ".mobile-nav{display:none}" in CSS
    tail = CSS[CSS.index("INTERACTION POLISH"):]
    assert "@media(max-width:900px)" in tail and ".mobile-nav{display:flex" in tail
    assert "@media(prefers-reduced-motion:reduce)" in tail
    assert 'class="mobile-nav"' in JS


def test_the_household_tour_and_its_controls_use_the_catalogue():
    steps = JS[JS.index("function householdTourSteps"):JS.index("/* The invitation.")]
    assert steps.count('title: t("tour.household.') == steps.count("title:")
    paint = JS[JS.index("function tourPaint"):JS.index("function tourEnd")]
    for key in ("tour.progress", "tour.skip", "tour.back", "tour.finish", "tour.next"):
        assert f't("{key}"' in paint
