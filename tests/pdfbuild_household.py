"""Generated synthetic documents for the Rao household portfolio example."""
from __future__ import annotations

from pathlib import Path

from pdfbuild import SYNTHETIC_MARK, build_pdf


def household_profile_pdf() -> bytes:
    return build_pdf([[
        "RAO HOUSEHOLD PROFILE",
        "SYNTHETIC DECLARATION FOR DEMONSTRATION",
        "",
        "Household members:",
        "Arjun Rao, age 38; Meera Rao, age 36; Anya Rao, age 4.",
        "Meera's parents live in Bengaluru: Raghavan, age 67; Lakshmi, age 63.",
        "Lakshmi has declared diabetes for the family health policy record.",
        "",
        "Assets recorded for this synthetic household:",
        "Singapore family car; Bengaluru family car; Singapore home contents.",
        "No nominee is recorded in this synthetic household profile.",
        "",
        "This profile is self-authored and is not policy wording.",
    ]], banner=SYNTHETIC_MARK)


def singapore_health_schedule_pdf() -> bytes:
    return build_pdf([[
        "SYNTHETIC HEALTH POLICY SCHEDULE",
        "",
        "Insured members: Arjun Rao and Meera Rao",
        "MediShield Life schedule reference: ****-ML01",
        "Singlife Shield Standard Plan schedule reference: ****-ISP2",
        "Period: 1 January 2026 to 31 December 2026",
        "Synthetic schedule sum insured: SGD 1,500,000",
        "Hospitalisation benefits are listed for both insured members.",
        "Waiting periods, co-payment and exclusions require the policy wording.",
    ]], banner=SYNTHETIC_MARK)


def home_contents_schedule_pdf() -> bytes:
    return build_pdf([[
        "SYNTHETIC HOME CONTENTS POLICY SCHEDULE",
        "",
        "Insured household: Arjun Rao and Meera Rao",
        "Policy reference: ****-HOME3",
        "Period: 1 February 2026 to 31 January 2027",
        "Home contents sum insured: SGD 300,000",
        "Synthetic contents list estimate: SGD 420,000",
        "Policy conditions and exclusions require the policy wording.",
    ]], banner=SYNTHETIC_MARK)


def singapore_motor_travel_schedule_pdf() -> bytes:
    return build_pdf([[
        "SYNTHETIC MOTOR AND TRAVEL POLICY SCHEDULES",
        "",
        "Singapore private car plan reference: ****-MOTOR4",
        "Insured asset: Singapore family car",
        "Motor period: 1 March 2026 to 28 February 2027",
        "",
        "Travel plan reference: ****-TRAVEL5",
        "Travellers: Arjun Rao, Meera Rao and Anya Rao",
        "Travel period: 1 June 2026 to 31 May 2027",
        "Further limits and conditions require the policy wording.",
    ]], banner=SYNTHETIC_MARK)


def india_health_schedule_pdf() -> bytes:
    return build_pdf([[
        "SYNTHETIC INDIA FAMILY FLOATER HEALTH SCHEDULE",
        "",
        "Product: Arogya Sanjeevani Policy",
        "Insured members: Raghavan (age 67) and Lakshmi (age 63)",
        "Policy reference: ****-HEALTH6",
        "Period: 1 April 2026 to 31 March 2027",
        "Synthetic family-floater sum insured: INR 500,000.",
        "Lakshmi has declared diabetes.",
        "Room-rent, co-payment and waiting-period terms require the policy wording.",
    ]], banner=SYNTHETIC_MARK)


def india_motor_schedule_pdf() -> bytes:
    return build_pdf([[
        "SYNTHETIC INDIA MOTOR POLICY SCHEDULE",
        "",
        "Insured asset: Bengaluru family car",
        "Policy reference: ****-MOTOR7",
        "Policy period: 19 October 2025 to 18 October 2026",
        "Third-party cover expiry: 18 October 2026",
        "Depreciation, deductibles and conditions require the policy wording.",
    ]], banner=SYNTHETIC_MARK)


BUILDERS = {
    "rao-household-profile.pdf": household_profile_pdf,
    "rao-singapore-health-schedule.pdf": singapore_health_schedule_pdf,
    "rao-home-contents-schedule.pdf": home_contents_schedule_pdf,
    "rao-singapore-motor-travel-schedule.pdf": singapore_motor_travel_schedule_pdf,
    "rao-india-health-schedule.pdf": india_health_schedule_pdf,
    "rao-india-motor-schedule.pdf": india_motor_schedule_pdf,
}


def regenerate_all(dest: Path) -> list[str]:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for name, builder in BUILDERS.items():
        (dest / name).write_bytes(builder())
        written.append(name)
    return written


if __name__ == "__main__":
    here = Path(__file__).resolve()
    names = regenerate_all(here.parents[1] / "demo-documents" / "household")
    print("wrote", ", ".join(names))
