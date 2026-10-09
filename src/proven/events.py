"""Change events — the trigger side of the product (spec §7).

Nine internal change events, as buttons rather than a form. Each one names the rules
it makes relevant and the documents it asks for, so tapping an event tells the owner
what to upload rather than opening a questionnaire.

WHAT AN EVENT IS NOT. An event is a statement by the owner, so it is SELF-authored
and it can never upgrade a finding's evidence class or create a fact. It selects
which findings are relevant, is written to the audit log, and nothing else. If an
event could establish a value, "the business told us" would launder into PROVEN,
which is the exact failure the grading scheme exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChangeEvent:
    code: str
    label: str
    asks_for: tuple[str, ...]       # document kinds this event makes worth having
    relevant_rules: tuple[str, ...]  # rule ids the event points at
    note: str = ""


EVENTS: tuple[ChangeEvent, ...] = (
    ChangeEvent(
        "STARTED_SELLING_ONLINE", "Started selling online",
        ("ACRA_PROFILE", "SCHEDULE"),
        ("ACTIVITY-SSIC-NOT-DECLARED",),
        "A new sales channel usually shows up as a secondary SSIC activity before it "
        "shows up on the policy.",
    ),
    ChangeEvent(
        "OPENED_OR_LEASED_LOCATION", "Opened or leased a location",
        ("TENANCY", "SCHEDULE", "ENDORSEMENT"),
        ("ALTERATION-PREMISES-NOT-ON-SCHEDULE", "MINIMUM-SECURITY",
         "UNOCCUPANCY-WATER-EXCLUSION"),
        "The strongest finding in the corpus: premises on the lease but not on the "
        "schedule.",
    ),
    ChangeEvent(
        "BOUGHT_COSTLY_EQUIPMENT", "Bought costly equipment",
        ("SUPPLIER_INVOICE", "SCHEDULE"),
        ("PLANT-ADEQUACY-UNESTABLISHED", "AVERAGE-ADEQUACY-BELOW-THRESHOLD"),
        "New plant moves the value at risk without moving the sum insured.",
    ),
    ChangeEvent(
        "ADDED_KEY_SUPPLIER_OR_CUSTOMER", "Added a key supplier or customer",
        ("SCHEDULE",),
        ("BUSINESS-INTERRUPTION-NOT-CONFIRMED",),
        "Concentration risk is a business-interruption question, and BI is often "
        "absent from a packaged policy.",
    ),
    ChangeEvent(
        "HEADCOUNT_CHANGE", "Headcount changed",
        ("CPF_RECORD", "WIC_SCHEDULE", "WORK_PASS_LIST"),
        ("WICA-HEADCOUNT-UNDERDECLARED", "WORKPASS-MEDICAL-BELOW-MINIMUM"),
        "Compulsory work injury cover tracks actual headcount, not declared "
        "headcount.",
    ),
    ChangeEvent(
        "MOVED_TO_BESPOKE_COVER", "Moved from a packaged policy to bespoke cover",
        ("BASE_WORDING", "SCHEDULE", "ENDORSEMENT"),
        ("AVERAGE-ADEQUACY-BELOW-THRESHOLD", "SUBLIMIT-CONFLICTING-ENDORSEMENTS"),
        "A new wording resets every threshold. Nothing carries over.",
    ),
    ChangeEvent(
        "STARTED_HANDLING_CUSTOMER_DATA", "Started handling customer data",
        ("BASE_WORDING", "SCHEDULE"),
        ("BUSINESS-INTERRUPTION-NOT-CONFIRMED",),
        "Cyber and data exposure is usually excluded rather than absent, and the "
        "exclusion is what has to be read.",
    ),
    ChangeEvent(
        "WON_LARGER_CONTRACT", "Won a contract with a larger company",
        ("SCHEDULE", "BASE_WORDING"),
        ("CLAIM-TIME-LIMIT", "BUILDINGS-VALUE-UNESTABLISHED"),
        "The certificate-of-insurance trigger: a counterparty's requirements are "
        "tested against the limits actually held.",
    ),
    ChangeEvent(
        "CHANGED_PREMISES_USE", "Changed what the premises are used for",
        ("TENANCY", "ACRA_PROFILE", "SCHEDULE"),
        ("ALTERATION-PREMISES-NOT-ON-SCHEDULE", "ACTIVITY-SSIC-NOT-DECLARED"),
        "Grace Electrical [2016] SGHC 233. The most consequential event in the "
        "list.",
    ),
)

BY_CODE: dict[str, ChangeEvent] = {e.code: e for e in EVENTS}


def catalogue() -> list[dict]:
    """The event list, for the UI. Data in, data out — no server-side rendering."""
    return [
        {"code": e.code, "label": e.label, "asks_for": list(e.asks_for),
         "relevant_rules": list(e.relevant_rules), "note": e.note}
        for e in EVENTS
    ]


def validate(codes: list[str]) -> list[str]:
    """Keep only known codes, preserving order and dropping duplicates. Unknown
    codes are discarded rather than stored: the event vocabulary is closed."""
    seen: set[str] = set()
    out: list[str] = []
    for code in codes or []:
        key = str(code).strip().upper()
        if key in BY_CODE and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def rules_for(codes: list[str]) -> set[str]:
    """Rule ids made relevant by these events."""
    out: set[str] = set()
    for code in validate(codes):
        out.update(BY_CODE[code].relevant_rules)
    return out


def documents_for(codes: list[str]) -> list[str]:
    """Document kinds worth uploading given these events."""
    out: list[str] = []
    for code in validate(codes):
        for kind in BY_CODE[code].asks_for:
            if kind not in out:
                out.append(kind)
    return out
