"""Synthetic household example data for the T06 portfolio flow.

The browser uploads the generated PDFs through the ordinary intake pipeline first.  The
server validates those uploaded bytes against the generated files before this module is
used to persist the matching, explicitly synthetic household.  Keeping the data here
makes the example reproducible without treating it as a pre-populated customer record.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SEED_ID = "rao-household-v1"
HOUSEHOLD_NAME = "Rao household (synthetic)"


@dataclass(frozen=True)
class DocumentSpec:
    filename: str
    label: str
    country: str
    kind: str


@dataclass(frozen=True)
class MemberSpec:
    key: str
    name: str
    relationship: str
    date_of_birth: str
    health_facts: Optional[str] = None


@dataclass(frozen=True)
class AssetSpec:
    key: str
    kind: str
    name: str


@dataclass(frozen=True)
class CoverageSpec:
    mechanism_code: str
    value: Optional[str]
    unit: Optional[str]
    wording: str


@dataclass(frozen=True)
class HealthScheduleSpec:
    """Exact, generated schedule spans used as operands by the health mapper.

    These values are not caller input. The loader reaches this fixture only after the
    uploaded PDF bytes match the generator, then binds the policy to that retained
    source document. The mapper keeps the resulting source-document id and span id on
    every derived item so a public wording never obscures the schedule operand.
    """

    policy_start_quote: str
    insured_members_quote: str
    member_ages: tuple[tuple[str, int], ...] = ()
    family_floater: bool = False
    family_floater_quote: Optional[str] = None
    declared_member_key: Optional[str] = None
    declared_condition: Optional[str] = None
    declared_condition_quote: Optional[str] = None


@dataclass(frozen=True)
class PolicySpec:
    country: str
    line_of_business: str
    insurer: str
    product: str
    masked_number: str
    period_start: str
    period_end: str
    status: str
    renewal_date: Optional[str]
    member_keys: tuple[str, ...]
    asset_keys: tuple[str, ...]
    document_filename: str
    coverage: tuple[CoverageSpec, ...]
    health_schedule: Optional[HealthScheduleSpec] = None
    #: Exact single lines of the generated schedule that establish a home or travel
    #: operand, keyed by operand name. The mapper parses every number from these lines
    #: and cites them; nothing here is a value the mapper may use without its quote.
    schedule_quotes: tuple[tuple[str, str], ...] = ()
    #: What the schedule insures when it is a property not modelled as an asset.
    property_label: Optional[str] = None


@dataclass(frozen=True)
class ContentsListSpec:
    """The owner's own contents list, as written in the self-declared household profile.

    Self-authored, so every figure derived from it is DECLARED. The mapper adds the item
    lines itself and compares the result with the stated total; a disagreement yields no
    figure at all rather than either number.
    """

    document_filename: str
    for_masked_number: str
    item_lines: tuple[str, ...]
    total_line: str


DOCUMENTS: tuple[DocumentSpec, ...] = (
    DocumentSpec("rao-household-profile.pdf", "Household profile", "GENERIC", "SELF_DECLARATION"),
    DocumentSpec("rao-singapore-health-schedule.pdf", "Singapore health schedules", "SG", "SCHEDULE"),
    DocumentSpec("rao-home-contents-schedule.pdf", "Home contents schedule", "SG", "SCHEDULE"),
    DocumentSpec("rao-singapore-motor-travel-schedule.pdf", "Singapore motor and travel schedules", "SG", "SCHEDULE"),
    DocumentSpec("rao-india-health-schedule.pdf", "India family health schedule", "IN", "SCHEDULE"),
    DocumentSpec("rao-india-motor-schedule.pdf", "India motor and home schedules", "IN", "SCHEDULE"),
)

MEMBERS: tuple[MemberSpec, ...] = (
    MemberSpec("arjun", "Arjun Rao", "self", "1988-05-14"),
    MemberSpec("meera", "Meera Rao", "spouse", "1990-09-22"),
    MemberSpec("anya", "Anya Rao", "daughter", "2022-04-03"),
    MemberSpec("raghavan", "Raghavan", "parent", "1959-03-18"),
    MemberSpec("lakshmi", "Lakshmi", "parent", "1963-06-11", "Declared diabetes"),
)

ASSETS: tuple[AssetSpec, ...] = (
    AssetSpec("sg-car", "CAR", "Singapore family car"),
    AssetSpec("in-car", "CAR", "Bengaluru family car"),
    AssetSpec("home-contents", "HOME_CONTENTS", "Singapore home contents"),
)

POLICIES: tuple[PolicySpec, ...] = (
    PolicySpec(
        country="SG", line_of_business="HEALTH", insurer="MediShield Life",
        product="MediShield Life (synthetic schedule)", masked_number="****-ML01",
        period_start="2026-01-01", period_end="2026-12-31", status="ACTIVE",
        renewal_date="2026-12-31", member_keys=("arjun", "meera"), asset_keys=(),
        document_filename="rao-singapore-health-schedule.pdf",
        coverage=(
            CoverageSpec("COVERED.HOSPITALISATION", None, None,
                         "Hospitalisation benefits are listed for both insured members."),
            CoverageSpec("LIMIT.SUM_INSURED", "1500000", "SGD",
                         "Synthetic schedule sum insured: SGD 1,500,000"),
        ),
    ),
    PolicySpec(
        country="SG", line_of_business="HEALTH", insurer="Singlife",
        product="Singlife Shield Standard Plan (synthetic schedule)", masked_number="****-ISP2",
        period_start="2026-01-01", period_end="2026-12-31", status="ACTIVE",
        renewal_date="2026-12-31", member_keys=("arjun", "meera"), asset_keys=(),
        document_filename="rao-singapore-health-schedule.pdf",
        coverage=(
            CoverageSpec("COVERED.HOSPITALISATION", None, None,
                         "Singlife Shield Standard Plan schedule reference: ****-ISP2"),
            CoverageSpec("LIMIT.SUM_INSURED", "1000000", "SGD",
                         "Synthetic separate plan sum insured: SGD 1,000,000"),
        ),
    ),
    PolicySpec(
        country="SG", line_of_business="HOME", insurer="AIG Asia Pacific Insurance Pte. Ltd.",
        product="AIG Homes Advantage (synthetic schedule)", masked_number="****-HOME3",
        period_start="2026-02-01", period_end="2027-01-31", status="ACTIVE",
        renewal_date="2027-01-31", member_keys=("arjun", "meera"), asset_keys=("home-contents",),
        document_filename="rao-home-contents-schedule.pdf",
        coverage=(
            CoverageSpec("COVERED.HOME_CONTENTS", None, None,
                         "Cover selected: Household Contents"),
            CoverageSpec("LIMIT.SUM_INSURED", "300000", "SGD",
                         "Home contents sum insured: SGD 300,000"),
        ),
        schedule_quotes=(
            ("contents_sum_insured", "Home contents sum insured: SGD 300,000"),
        ),
        property_label="Singapore home contents",
    ),
    PolicySpec(
        country="SG", line_of_business="MOTOR", insurer="Lotus General (Sandbox)",
        product="Private car plan (synthetic schedule)", masked_number="****-MOTOR4",
        period_start="2026-03-01", period_end="2027-02-28", status="ACTIVE",
        renewal_date="2027-02-28", member_keys=("arjun", "meera"), asset_keys=("sg-car",),
        document_filename="rao-singapore-motor-travel-schedule.pdf",
        coverage=(
            CoverageSpec("COVERED.MOTOR_THIRD_PARTY", None, None,
                         "Insured asset: Singapore family car"),
        ),
    ),
    PolicySpec(
        country="SG", line_of_business="TRAVEL", insurer="AIG Asia Pacific Insurance Pte. Ltd.",
        product="AIG Travel Guard Direct (synthetic schedule)", masked_number="****-TRAVEL5",
        period_start="2026-06-01", period_end="2027-05-31", status="ACTIVE",
        renewal_date="2027-05-31", member_keys=("arjun", "meera", "anya"), asset_keys=(),
        document_filename="rao-singapore-motor-travel-schedule.pdf",
        coverage=(
            CoverageSpec("COVERED.TRAVEL", None, None,
                         "Travellers: Arjun Rao, Meera Rao and Anya Rao"),
        ),
        schedule_quotes=(
            ("travellers", "Travellers: Arjun Rao, Meera Rao and Anya Rao"),
            ("selected_plan", "Selected plan: not stated on this synthetic schedule"),
        ),
    ),
    PolicySpec(
        country="IN", line_of_business="HEALTH", insurer="Go Digit General Insurance Limited",
        product="Arogya Sanjeevani Policy (synthetic schedule)", masked_number="****-HEALTH6",
        period_start="2026-04-01", period_end="2027-03-31", status="ACTIVE",
        renewal_date="2027-03-31", member_keys=("raghavan", "lakshmi"), asset_keys=(),
        document_filename="rao-india-health-schedule.pdf",
        coverage=(
            CoverageSpec("COVERED.FAMILY_FLOATER", None, None,
                         "Synthetic family-floater sum insured: INR 500,000."),
            CoverageSpec("LIMIT.SUM_INSURED", "500000", "INR",
                         "Synthetic family-floater sum insured: INR 500,000."),
            CoverageSpec("WAITING.PED", None, None,
                         "Lakshmi has declared diabetes."),
            CoverageSpec("SHARE.CO_PAYMENT", None, None,
                         "Room-rent, co-payment and waiting-period terms require the policy wording."),
        ),
        health_schedule=HealthScheduleSpec(
            policy_start_quote=(
                "Period: 1 April 2026 to 31 March 2027\n"
                "First policy commencement: 1 April 2026.\n"
                "Continuous coverage before this period: 0 months.\n"
                "Portability or migration credit: 0 months.\n"
                "Sum-insured enhancement: none."
            ),
            insured_members_quote="Insured members: Raghavan (age 67) and Lakshmi (age 63)",
            member_ages=(("raghavan", 67), ("lakshmi", 63)),
            family_floater=True,
            family_floater_quote="Synthetic family-floater sum insured: INR 500,000.",
            declared_member_key="lakshmi",
            declared_condition="diabetes",
            declared_condition_quote="Lakshmi has declared diabetes.",
        ),
    ),
    PolicySpec(
        country="IN", line_of_business="MOTOR", insurer="Lotus General (Sandbox)",
        product="Private car plan (synthetic schedule)", masked_number="****-MOTOR7",
        period_start="2025-10-19", period_end="2026-10-18", status="ACTIVE",
        renewal_date="2026-10-18", member_keys=("raghavan", "lakshmi"), asset_keys=("in-car",),
        document_filename="rao-india-motor-schedule.pdf",
        coverage=(
            CoverageSpec("COVERED.MOTOR_THIRD_PARTY", None, None,
                         "Insured asset: Bengaluru family car"),
            CoverageSpec("RENEWAL.THIRD_PARTY", None, None,
                         "Third-party cover expiry: 18 October 2026"),
            CoverageSpec("CONDITION.DEPRECIATION_SCHEDULE", None, None,
                         "Depreciation schedule: plastic parts 50%; metal parts 10% for a vehicle aged up to five years."),
        ),
    ),
    # Appended, never inserted: a persisted seed binding records each policy's index.
    PolicySpec(
        country="IN", line_of_business="HOME", insurer="HDFC ERGO General Insurance Company Limited",
        product="HDFC ERGO Bharat Griha Raksha (synthetic schedule)", masked_number="****-HOME8",
        period_start="2026-04-01", period_end="2027-03-31", status="ACTIVE",
        renewal_date="2027-03-31", member_keys=("raghavan", "lakshmi"), asset_keys=(),
        document_filename="rao-india-motor-schedule.pdf",
        coverage=(
            CoverageSpec("COVERED.HOME_BUILDING", None, None,
                         "Cover selected: Home Building and General Contents"),
            CoverageSpec("LIMIT.SUM_INSURED", "4500000", "INR",
                         "Home Building sum insured (declared cost of construction): INR 4,500,000"),
        ),
        schedule_quotes=(
            ("building_sum_insured",
             "Home Building sum insured (declared cost of construction): INR 4,500,000"),
        ),
        property_label="Bengaluru family home",
    ),
)

CONTENTS_LIST = ContentsListSpec(
    document_filename="rao-household-profile.pdf",
    for_masked_number="****-HOME3",
    item_lines=(
        "Furniture SGD 95,000; electronics and appliances SGD 88,000;",
        "jewellery and watches SGD 162,000; clothing and personal effects SGD 45,000;",
        "art and collectibles SGD 30,000.",
    ),
    total_line="Contents list total (self-declared): SGD 420,000.",
)


def document_names() -> tuple[str, ...]:
    return tuple(document.filename for document in DOCUMENTS)


def evidence_for_seeded_policy(
    seed_id: str, policy_index: object, mechanism_code: str,
) -> tuple[PolicySpec, CoverageSpec] | None:
    """Return a reviewed quotation only for an immutable seeded policy binding."""
    if seed_id != SEED_ID or isinstance(policy_index, bool) or not isinstance(policy_index, int):
        return None
    if policy_index < 0 or policy_index >= len(POLICIES):
        return None
    policy = POLICIES[policy_index]
    for coverage in policy.coverage:
        if coverage.mechanism_code == mechanism_code:
            return policy, coverage
    return None
